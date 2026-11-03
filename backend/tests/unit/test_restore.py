"""The restore (``vault.storage.restore``) and its command line (``python -m vault.restore``).

Every vault, backup and target is a temporary folder with synthetic documents.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from vault import restore as cli
from vault.check import check_vault
from vault.core.config import Settings
from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.ingest.importer import Importer, ImportRequest
from vault.main import create_app
from vault.storage import acl
from vault.storage import restore as restore_module
from vault.storage.backup import BackupError, run_backup
from vault.storage.blobs import READ_ONLY, BlobIntegrityError, BlobStore
from vault.storage.restore import RestoreError, RestoreResult, run_restore

from .test_backup import (
    CONFIG_TEXT,
    DOCUMENTS,
    PRIVATE_NAME,
    START,
    TOKEN_TEXT,
    clock,
    link,
    sha,
)

LOCAL = "http://localhost"
OWNER_RW = 0o666 if os.name == "nt" else 0o600  # Windows keeps only a read-only attribute


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def make_vault(root: Path, incoming: Path) -> Path:
    root.mkdir(parents=True)
    upgrade_to_latest(root / "vault.db")
    engine = create_vault_engine(root / "vault.db")
    try:
        store = BlobStore(root / "blobs")
        incoming.mkdir()
        for name, data in DOCUMENTS.items():
            (incoming / name).write_bytes(data)
            outcome = Importer(engine, store).import_file(
                ImportRequest(
                    path=incoming / name,
                    original_filename=name,
                    method="upload",
                    batch_id=ids.new_ulid(),
                )
            )
            assert outcome.reason is None
    finally:
        engine.dispose()
    (root / "local-token").write_text(TOKEN_TEXT)
    (root / "local-token").chmod(0o600)
    return root


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return make_vault(tmp_path / "vault", tmp_path / "incoming")


@pytest.fixture
def backup(vault: Path, tmp_path: Path) -> Path:
    """A backup folder with one snapshot of the vault, its settings and its token."""
    folder = tmp_path / "backups"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TEXT)
    run_backup(vault, folder, config_file=config, now=clock())
    return folder


@pytest.fixture
def snapshot(backup: Path) -> Path:
    return next((backup / "snapshots").iterdir())


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "restored" / PRIVATE_NAME


def work_folders(parent: Path) -> list[Path]:
    if not parent.exists():
        return []
    return [p for p in parent.iterdir() if p.name.startswith(".vault-restore-")]


def blob_in_backup(backup: Path, data: bytes) -> Path:
    found = BlobStore(backup / "blobs").find(sha(data))
    assert found is not None
    return found


def rewrite(path: Path, data: bytes) -> None:
    path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(READ_ONLY)


class TestARestore:
    def test_it_restores_the_database_the_blobs_the_token_and_the_settings(
        self, snapshot: Path, target: Path
    ) -> None:
        result = run_restore(snapshot, target)
        assert result.snapshot == snapshot.name
        assert (result.restored, result.problems, result.leftovers) == (3, 0, 0)
        assert (result.token_state, result.config) == ("restored", True)
        assert sorted(p.name for p in target.iterdir()) == [
            "blobs",
            "config.toml",
            "local-token",
            "vault.db",
        ]
        assert (target / "vault.db").read_bytes() == (snapshot / "vault.db").read_bytes()
        assert (target / "local-token").read_text() == TOKEN_TEXT
        assert (target / "config.toml").read_text() == CONFIG_TEXT
        assert mode(target / "vault.db") == OWNER_RW
        assert mode(target / "config.toml") == OWNER_RW
        assert mode(target / "local-token") == OWNER_RW
        store = BlobStore(target / "blobs")
        for data in DOCUMENTS.values():
            found = store.find(sha(data))
            assert found is not None
            assert found.read_bytes() == data
            assert mode(found) == READ_ONLY  # exactly, not only without the owner's write bit
        assert work_folders(target.parent) == []

    def test_the_restored_vault_passes_the_integrity_check(
        self, snapshot: Path, target: Path
    ) -> None:
        run_restore(snapshot, target)
        report = check_vault(target)
        assert report.problems == 0
        assert report.blobs_checked == len(DOCUMENTS)

    def test_the_app_starts_on_the_restored_vault_with_the_same_token(
        self, snapshot: Path, target: Path
    ) -> None:
        run_restore(snapshot, target)
        app = create_app(Settings.model_validate({"vault": {"path": str(target)}}))
        assert app.state.token == TOKEN_TEXT
        headers = {"Authorization": f"Bearer {TOKEN_TEXT}", "Origin": LOCAL}
        with TestClient(app, base_url=LOCAL, headers=headers) as client:
            response = client.get("/api/documents")
        assert response.status_code == 200
        assert response.json()["total"] == len(DOCUMENTS)
        # Start-up made the folders a restore leaves out and found no orphans to move aside.
        assert (target / "derived").is_dir()
        assert not (target / "blobs" / "orphans").exists()

    def test_the_backup_folder_means_its_latest_complete_snapshot(
        self, vault: Path, backup: Path, target: Path
    ) -> None:
        later = run_backup(vault, backup, now=clock(START + timedelta(hours=1)))
        (backup / "snapshots" / "2099-01-01T000000Z").mkdir()  # newer, but did not finish
        result = run_restore(backup, target)
        assert result.snapshot == later.snapshot
        assert result.config is False  # the later backup was run without a settings file
        assert not (target / "config.toml").exists()

    def test_an_empty_target_folder_is_used(self, snapshot: Path, target: Path) -> None:
        target.mkdir(parents=True)
        assert run_restore(snapshot, target).problems == 0
        assert (target / "vault.db").is_file()

    def test_an_empty_configured_vault_folder_can_be_the_target(
        self, snapshot: Path, tmp_path: Path
    ) -> None:
        """A replaced drive: the settings still name the folder, which is now empty."""
        replaced = tmp_path / "new-drive" / "AIVault"
        replaced.mkdir(parents=True)
        run_restore(snapshot, replaced, vault_root=replaced)
        assert (replaced / "vault.db").is_file()

    def test_a_snapshot_without_a_token_restores_without_one(
        self, snapshot: Path, target: Path
    ) -> None:
        (snapshot / "local-token").chmod(0o600)
        (snapshot / "local-token").unlink()
        result = run_restore(snapshot, target)
        assert result.token_state == "absent"  # noqa: S105 - a state, not a secret
        assert not (target / "local-token").exists()

    @pytest.mark.parametrize("data", [b"short", b"x" * 2000, TOKEN_TEXT.encode() + b"!"])
    def test_a_token_start_up_would_refuse_is_left_out(
        self, snapshot: Path, target: Path, data: bytes
    ) -> None:
        rewrite(snapshot / "local-token", data)
        result = run_restore(snapshot, target)
        assert result.token_state == "invalid"  # noqa: S105 - a state, not a secret
        assert not (target / "local-token").exists()

    def test_a_settings_file_far_too_large_is_refused(self, snapshot: Path, target: Path) -> None:
        rewrite(snapshot / "config.toml", b"#" * (restore_module.CONFIG_MAX_BYTES + 1))
        with pytest.raises(RestoreError, match="settings file"):
            run_restore(snapshot, target)
        assert not target.exists()
        assert work_folders(target.parent) == []


class TestFilesThatCannotBeRestored:
    def test_a_missing_blob_is_counted_and_the_rest_is_restored(
        self, backup: Path, snapshot: Path, target: Path
    ) -> None:
        BlobStore(backup / "blobs").delete(sha(DOCUMENTS["notes.md"]))
        result = run_restore(snapshot, target)
        assert (result.restored, result.missing, result.problems) == (2, 1, 1)
        assert BlobStore(target / "blobs").find(sha(DOCUMENTS["notes.md"])) is None
        assert check_vault(target).counts == {"version_blob_missing": 1}

    @pytest.mark.parametrize(
        "data",
        [
            b"# Lab notes\n\nThe sample was weighed three times.\n",  # other bytes
            DOCUMENTS["notes.md"][:-1],  # a shorter copy
            DOCUMENTS["notes.md"] + b"\n",  # a longer one
        ],
    )
    def test_a_blob_that_does_not_match_its_hash_is_not_restored(
        self, backup: Path, snapshot: Path, target: Path, data: bytes
    ) -> None:
        rewrite(blob_in_backup(backup, DOCUMENTS["notes.md"]), data)
        result = run_restore(snapshot, target)
        assert (result.restored, result.damaged) == (2, 1)
        assert BlobStore(target / "blobs").scan().blobs != ()
        assert BlobStore(target / "blobs").find(sha(DOCUMENTS["notes.md"])) is None

    def test_a_linked_blob_is_not_read(
        self, backup: Path, snapshot: Path, target: Path, tmp_path: Path
    ) -> None:
        blob = blob_in_backup(backup, DOCUMENTS["notes.md"])
        outside = tmp_path / "outside.md"
        outside.write_bytes(DOCUMENTS["notes.md"])
        blob.chmod(0o644)
        blob.unlink()
        link(outside, blob)
        result = run_restore(snapshot, target)
        assert result.missing == 1
        assert BlobStore(target / "blobs").find(sha(DOCUMENTS["notes.md"])) is None

    def test_an_unreadable_blob_is_counted(
        self, backup: Path, snapshot: Path, target: Path
    ) -> None:
        real_open = Path.open
        blob = blob_in_backup(backup, DOCUMENTS["notes.md"])

        def refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == blob:
                raise PermissionError(13, "held by another program", str(self))
            return real_open(self, *args, **kwargs)

        with mock.patch.object(Path, "open", refuse):
            result = run_restore(snapshot, target)
        assert (result.restored, result.unreadable) == (2, 1)


class TestNothingHalfRestoredIsLeft:
    def test_a_damaged_database_is_refused_and_nothing_is_left(
        self, snapshot: Path, target: Path
    ) -> None:
        data = bytearray((snapshot / "vault.db").read_bytes())
        data[4096:8192] = b"\xff" * 4096  # a page of nonsense
        rewrite(snapshot / "vault.db", bytes(data))
        with pytest.raises(RestoreError, match="integrity check"):
            run_restore(snapshot, target)
        assert not target.exists()
        assert work_folders(target.parent) == []

    def test_an_interruption_removes_the_work_and_leaves_the_target_as_it_was(
        self, snapshot: Path, target: Path
    ) -> None:
        target.mkdir(parents=True)
        with (
            mock.patch.object(restore_module, "_copy_blobs", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            run_restore(snapshot, target)
        assert list(target.iterdir()) == []
        assert work_folders(target.parent) == []

    def test_until_the_end_nothing_is_under_the_targets_name(
        self, snapshot: Path, target: Path
    ) -> None:
        seen: list[bool] = []
        real = restore_module._move_into_place

        def move(work: Path, final: Path) -> None:
            seen.append(final.exists())
            assert (work / "vault.db").is_file()
            assert (work / "local-token").is_file()
            real(work, final)

        with mock.patch.object(restore_module, "_move_into_place", move):
            run_restore(snapshot, target)
        assert seen == [False]

    def test_a_killed_run_leaves_a_work_folder_that_the_next_one_counts(
        self, snapshot: Path, target: Path
    ) -> None:
        target.parent.mkdir(parents=True)
        leftover = target.parent / (".vault-restore-" + "a" * 32)
        leftover.mkdir()
        (leftover / "vault.db").write_bytes(b"half")
        result = run_restore(snapshot, target)
        assert result.leftovers == 1
        assert (leftover / "vault.db").read_bytes() == b"half"  # counted, never removed

    def test_something_put_in_the_target_meanwhile_stops_the_move(
        self, snapshot: Path, target: Path
    ) -> None:
        real = restore_module._restore_into

        def meanwhile(work: Path, source: Path) -> RestoreResult:
            result = real(work, source)
            target.mkdir(parents=True)
            (target / "theirs.txt").write_text("someone else's file")
            return result

        with (
            mock.patch.object(restore_module, "_restore_into", meanwhile),
            pytest.raises(RestoreError, match="while the restore ran"),
        ):
            run_restore(snapshot, target)
        assert [p.name for p in target.iterdir()] == ["theirs.txt"]
        assert work_folders(target.parent) == []

    def test_a_rename_that_fails_puts_the_empty_target_back(
        self, snapshot: Path, target: Path
    ) -> None:
        target.mkdir(parents=True)
        with (
            mock.patch.object(Path, "rename", side_effect=OSError(39, "not empty")),
            pytest.raises(RestoreError, match="target's name"),
        ):
            run_restore(snapshot, target)
        assert target.is_dir()
        assert list(target.iterdir()) == []
        assert work_folders(target.parent) == []

    def test_a_rename_refused_by_the_system_is_reported(
        self, snapshot: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(restore_module, "RENAME_WAIT_SECONDS", 0.0)  # Windows tries again
        with (
            mock.patch.object(Path, "rename", side_effect=PermissionError(13, "denied")),
            pytest.raises(RestoreError, match="target's name"),
        ):
            run_restore(snapshot, target)
        assert not target.exists()
        assert work_folders(target.parent) == []

    def test_a_damaged_blob_store_stops_the_restore(self, snapshot: Path, target: Path) -> None:
        with (
            mock.patch.object(
                restore_module, "_copy_blobs", side_effect=BlobIntegrityError("a link")
            ),
            pytest.raises(RestoreError, match="blob store"),
        ):
            run_restore(snapshot, target)
        assert not target.exists()
        assert work_folders(target.parent) == []

    def test_the_clean_up_of_nothing_does_nothing(self, tmp_path: Path) -> None:
        restore_module._remove_tree(tmp_path / "never-made")
        assert list(tmp_path.iterdir()) == []

    def test_the_clean_up_never_follows_a_link(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not the restore's")
        (outside / "keep.txt").chmod(READ_ONLY)
        work = tmp_path / (".vault-restore-" + "b" * 32)
        (work / "blobs").mkdir(parents=True)
        link(outside, work / "blobs" / "linked-folder")
        link(outside / "keep.txt", work / "linked-file")
        os.link(outside / "keep.txt", work / "second-name")
        restore_module._remove_tree(work)
        assert (outside / "keep.txt").read_text() == "not the restore's"
        assert mode(outside / "keep.txt") == READ_ONLY  # the hard link's mode was not changed
        if os.name != "nt":  # Windows cannot remove a name of a read-only file, so it stays
            assert not work.exists()


class TestTheSourceAndTargetChecks:
    def test_a_relative_path_is_refused(self, snapshot: Path, target: Path) -> None:
        with pytest.raises(BackupError, match="absolute"):
            run_restore(Path("backups"), target)
        with pytest.raises(BackupError, match="absolute"):
            run_restore(snapshot, Path("restored"))

    def test_a_folder_that_is_not_a_backup_is_refused(self, vault: Path, target: Path) -> None:
        with pytest.raises(RestoreError, match="neither a backup folder"):
            run_restore(vault, target)

    def test_a_missing_source_is_refused(self, tmp_path: Path, target: Path) -> None:
        with pytest.raises(RestoreError, match="not found"):
            run_restore(tmp_path / "nothing", target)

    def test_an_unfinished_snapshot_is_refused(self, backup: Path, target: Path) -> None:
        unfinished = backup / "snapshots" / "2099-01-01T000000Z"
        unfinished.mkdir()
        with pytest.raises(RestoreError, match="did not finish"):
            run_restore(unfinished, target)

    def test_a_backup_with_no_finished_snapshot_is_refused(
        self, tmp_path: Path, target: Path
    ) -> None:
        (tmp_path / "empty-backup" / "snapshots" / "2099-01-01T000000Z").mkdir(parents=True)
        with pytest.raises(RestoreError, match="no complete snapshot"):
            run_restore(tmp_path / "empty-backup", target)

    def test_a_backup_without_a_blob_store_is_refused(
        self, backup: Path, snapshot: Path, target: Path, tmp_path: Path
    ) -> None:
        (backup / "blobs").rename(tmp_path / "moved-blobs")
        with pytest.raises(RestoreError, match="no blobs folder"):
            run_restore(snapshot, target)

    def test_a_target_that_holds_anything_is_refused_and_left_alone(
        self, snapshot: Path, target: Path
    ) -> None:
        target.mkdir(parents=True)
        (target / "vault.db").write_bytes(b"the owner's live vault")
        with pytest.raises(RestoreError, match="already holds files"):
            run_restore(snapshot, target)
        assert [p.name for p in target.iterdir()] == ["vault.db"]
        assert (target / "vault.db").read_bytes() == b"the owner's live vault"
        assert work_folders(target.parent) == []

    def test_a_file_as_the_target_is_refused(self, snapshot: Path, target: Path) -> None:
        target.parent.mkdir(parents=True)
        target.write_text("a file")
        with pytest.raises(BackupError, match="not a folder"):
            run_restore(snapshot, target)

    def test_a_target_inside_the_backup_folder_is_refused(
        self, backup: Path, snapshot: Path
    ) -> None:
        with pytest.raises(BackupError, match="inside the backup folder"):
            run_restore(snapshot, backup / "restored")

    def test_a_target_inside_the_configured_vault_is_refused(
        self, vault: Path, snapshot: Path
    ) -> None:
        with pytest.raises(BackupError, match="inside the vault folder"):
            run_restore(snapshot, vault / "restored", vault_root=vault)

    def test_a_target_inside_a_git_repository_is_refused(
        self, snapshot: Path, tmp_path: Path
    ) -> None:
        repository = tmp_path / "repository"
        (repository / ".git").mkdir(parents=True)
        with pytest.raises(BackupError, match="Git repository"):
            run_restore(snapshot, repository / "vault")
        assert sorted(p.name for p in repository.iterdir()) == [".git"]

    @pytest.mark.parametrize("level", ["source", "target"])
    def test_a_link_in_either_path_is_refused(
        self, backup: Path, tmp_path: Path, level: str
    ) -> None:
        if level == "source":
            source = link(backup, tmp_path / "linked-backup")
            target = tmp_path / "restored"
        else:
            source = backup
            (tmp_path / "real").mkdir()
            target = link(tmp_path / "real", tmp_path / "linked-parent") / "restored"
        with pytest.raises(BackupError, match="link or a junction"):
            run_restore(source, target)

    @pytest.mark.parametrize("name", ["vault.db", "local-token", "config.toml"])
    def test_a_link_as_a_snapshot_file_is_refused(
        self, snapshot: Path, target: Path, tmp_path: Path, name: str
    ) -> None:
        outside = tmp_path / ("outside-" + name)
        outside.write_bytes((snapshot / name).read_bytes())
        (snapshot / name).chmod(0o600)
        (snapshot / name).unlink()
        link(outside, snapshot / name)
        with pytest.raises(BackupError, match="not a file"):
            run_restore(snapshot, target)
        assert not target.exists()
        assert work_folders(target.parent) == []

    def test_a_linked_folder_in_the_backup_blob_store_is_refused(
        self, backup: Path, snapshot: Path, target: Path, tmp_path: Path
    ) -> None:
        moved = tmp_path / "moved-sha256"
        (backup / "blobs" / "sha256").rename(moved)
        link(moved, backup / "blobs" / "sha256")
        result = run_restore(snapshot, target)
        assert result.missing == len(DOCUMENTS)  # never read through the link


class TestTheVaultPermissions:
    def test_the_new_folder_is_made_private_before_anything_is_written(
        self, snapshot: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secured: list[tuple[Path, bool, list[str]]] = []
        walked: list[Path] = []

        def secure(folder: Path, *, created: bool) -> None:
            secured.append((folder, created, sorted(p.name for p in folder.iterdir())))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", secure)
        monkeypatch.setattr(acl, "verify_tree", walked.append)
        run_restore(snapshot, target)
        assert len(secured) == 1
        assert secured[0][1:] == (True, [])  # a new folder, hardened while empty
        assert secured[0][0].name.startswith(".vault-restore-")
        assert walked == [secured[0][0]]  # the whole tree, after every file was written

    def test_a_folder_that_cannot_be_made_private_is_refused(
        self, snapshot: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(folder: Path, *, created: bool) -> None:
            raise acl.VaultAccessError("open to others: " + str(folder))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", refuse)
        with pytest.raises(RestoreError, match="could not be made private") as error:
            run_restore(snapshot, target)
        assert PRIVATE_NAME not in str(error.value)
        assert not target.exists()
        assert work_folders(target.parent) == []

    def test_an_open_entry_below_the_new_folder_is_refused(
        self, snapshot: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def open_below(root: Path) -> None:
            raise acl.VaultAccessError("1 files below " + str(root))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda folder, *, created: None)
        monkeypatch.setattr(acl, "verify_tree", open_below)
        with pytest.raises(RestoreError, match="other accounts"):
            run_restore(snapshot, target)
        assert not target.exists()
        assert work_folders(target.parent) == []


@pytest.mark.skipif(os.name != "nt", reason="uses PowerShell and Windows access lists")
class TestRealWindowsVaultLists:  # pragma: no cover - runs on the Windows CI job only
    """The real access lists: conftest turns the Windows steps off, so the test turns them on."""

    def test_the_restored_vault_and_everything_in_it_is_private(
        self, snapshot: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        run_restore(snapshot, target)
        assert acl.is_private(acl.read_access(target))
        acl.verify_tree(target)  # raises if any entry below is open to another account


class TestTheCommandLine:
    @pytest.fixture
    def settings(self, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "settings.toml"
        path.write_text(f"[vault]\npath = {json.dumps(str(vault))}\n")  # a TOML basic string
        monkeypatch.setenv("AIVAULT_CONFIG", str(path))
        monkeypatch.delenv(cli.SOURCE_ENV_VAR, raising=False)
        monkeypatch.delenv(cli.TARGET_ENV_VAR, raising=False)
        return path

    @staticmethod
    def private_words(snapshot: Path, target: Path) -> list[str]:
        return [
            PRIVATE_NAME,
            str(target),
            str(snapshot.parent.parent),
            *DOCUMENTS,
            sha(DOCUMENTS["notes.md"]),
            TOKEN_TEXT,
        ]

    def test_it_prints_counts_and_never_a_name(
        self,
        settings: Path,
        snapshot: Path,
        target: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert cli.main(["--from", str(snapshot), "--to", str(target)]) == 0
        out, err = capsys.readouterr()
        assert f"Snapshot {snapshot.name} restored" in out
        assert "checked against their hash: 3" in out
        assert "local-token restored" in out
        assert "config.toml in the new vault folder" in out
        assert err == ""
        for word in self.private_words(snapshot, target):
            assert word not in out + err

    def test_the_folders_can_come_from_the_environment(
        self,
        settings: Path,
        backup: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(cli.SOURCE_ENV_VAR, str(backup))
        monkeypatch.setenv(cli.TARGET_ENV_VAR, str(target))
        assert cli.main([]) == 0
        assert "checked against their hash: 3" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("argv", "expected"), [([], "pass --from"), (["--from", "x"], "pass --to")]
    )
    def test_without_a_folder_it_says_how_to_give_one(
        self, settings: Path, argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(argv) == 1
        assert expected in capsys.readouterr().err

    def test_problems_are_counted_and_fail_the_run(
        self,
        settings: Path,
        backup: Path,
        snapshot: Path,
        target: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        BlobStore(backup / "blobs").delete(sha(DOCUMENTS["notes.md"]))
        assert cli.main(["--from", str(snapshot), "--to", str(target)]) == cli.EXIT_PROBLEMS
        out, err = capsys.readouterr()
        assert "not in the backup: 1" in out
        assert "some files could not be restored" in err
        for word in self.private_words(snapshot, target):
            assert word not in out + err

    def test_a_target_inside_the_configured_vault_is_refused_in_fixed_text(
        self, settings: Path, vault: Path, snapshot: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = vault / PRIVATE_NAME
        assert cli.main(["--from", str(snapshot), "--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "inside the vault folder" in err
        for word in self.private_words(snapshot, target):
            assert word not in err

    def test_an_error_this_package_does_not_own_shows_only_its_class(
        self, settings: Path, snapshot: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = FileNotFoundError(2, "No such file", str(target / "secret.txt"))
        with mock.patch.object(cli, "run_restore", side_effect=error):
            assert cli.main(["--from", str(snapshot), "--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "FileNotFoundError" in err
        for word in self.private_words(snapshot, target):
            assert word not in err

    def test_an_interruption_is_reported_without_a_traceback(
        self, settings: Path, snapshot: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with mock.patch.object(cli, "run_restore", side_effect=KeyboardInterrupt):
            assert cli.main(["--from", str(snapshot), "--to", str(target)]) == 130
        assert "interrupted" in capsys.readouterr().err

    def test_settings_that_cannot_be_read_are_named_by_class_only(
        self,
        snapshot: Path,
        target: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("AIVAULT_CONFIG", str(tmp_path / PRIVATE_NAME / "config.toml"))
        assert cli.main(["--from", str(snapshot), "--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "FileNotFoundError" in err
        assert PRIVATE_NAME not in err

    def test_settings_without_a_vault_still_restore(
        self,
        snapshot: Path,
        target: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A new PC: no vault is set up yet, and the backup is all there is."""
        path = tmp_path / "settings.toml"
        path.write_text("[server]\nport = 8765\n")
        monkeypatch.setenv("AIVAULT_CONFIG", str(path))
        assert cli.main(["--from", str(snapshot), "--to", str(target)]) == 0

    @pytest.mark.parametrize(
        "argv",
        [
            ["--from", "D:\\Backups\\" + PRIVATE_NAME, PRIVATE_NAME],  # an extra word
            ["--" + PRIVATE_NAME],  # an unknown option
            ["--to"],  # a missing value
            ["--fr", PRIVATE_NAME],  # an abbreviation
            [PRIVATE_NAME],  # a positional word
        ],
    )
    def test_a_malformed_command_line_never_repeats_what_was_typed(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == 2
        out, err = capsys.readouterr()
        assert "command line is not valid" in err
        assert PRIVATE_NAME not in out + err

    def test_the_report_names_every_problem_and_every_token_state(self) -> None:
        result = RestoreResult(
            snapshot="2026-10-17T143005Z", restored=1, missing=2, damaged=3, unreadable=4,
            token_state="invalid", config=False, leftovers=5,  # noqa: S106 - a state
        )  # fmt: skip
        text = "\n".join(cli.report(result))
        for figure in ("missing", "damaged", "unreadable", "leftovers"):
            assert str(getattr(result, figure)) in text
        assert "not a valid token" in text
        assert result.problems == 2 + 3 + 4
        assert set(cli.TOKEN_LINES) == {"restored", "absent", "invalid"}
