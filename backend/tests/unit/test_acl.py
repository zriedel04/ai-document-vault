"""The Windows access-list rules, tested through the pure parts and with fakes.

The calls to PowerShell run only on Windows (the Windows CI job covers them at the end of this
file); everything that decides whether a folder is "private" is plain text parsing and runs on
every system.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from vault.storage import acl, layout
from vault.storage.acl import (
    AccessList,
    VaultAccessError,
    is_private,
    parse_access_list,
    sddl_is_private,
    secure_vault_folder,
)

OWNER = "S-1-5-21-111-222-333-1001"
OTHER_USER = "S-1-5-21-111-222-333-1002"
# What Windows reports for a folder made by ``make_private``: protected, owner only.
PRIVATE = f"O:BAG:SYD:PAI(A;OICI;FA;;;{OWNER})"
# What a folder created under C:\ reports: inherited entries for Users and Authenticated Users.
OPEN = (
    f"O:BAG:SYD:AI(A;OICI;FA;;;{OWNER})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    "(A;OICI;0x1200a9;;;BU)(A;CI;LC;;CIIO;BU)(A;;0x1200a9;;;AU)"
)


def private(sddl: str) -> bool:
    return sddl_is_private(sddl, OWNER)


class TestSddlIsPrivate:
    def test_an_owner_only_list_is_private(self) -> None:
        assert private(PRIVATE)

    @pytest.mark.parametrize("ending", ["\n", "\r\n", "  \n", ""])
    def test_a_line_break_at_the_end_changes_nothing(self, ending: str) -> None:
        assert private(PRIVATE + ending)

    def test_system_and_administrators_are_allowed_to_stay(self) -> None:
        assert private(f"O:BAG:SYD:PAI(A;OICI;FA;;;{OWNER})(A;OICI;FA;;;SY)(A;;FA;;;BA)")

    def test_the_default_list_of_a_folder_under_the_drive_root_is_not_private(self) -> None:
        assert not private(OPEN)

    @pytest.mark.parametrize(
        "principal",
        [
            "WD",
            "BU",
            "BG",
            "AU",
            "AN",
            "IU",
            "NU",
            "S-1-1-0",
            "S-1-5-32-545",
            "S-1-5-32-546",
            "S-1-5-11",
            "s-1-1-0",
            "bu",
        ],
    )
    def test_any_broad_group_makes_it_not_private(self, principal: str) -> None:
        assert not private(f"{PRIVATE}(A;OICI;0x1200a9;;;{principal})")

    @pytest.mark.parametrize("kind", ["OA", "XA", "ZA"])
    def test_other_allow_kinds_count_too(self, kind: str) -> None:
        assert not private(f"{PRIVATE}({kind};;FA;;;WD)")

    def test_a_deny_entry_for_a_broad_group_does_not_count(self) -> None:
        assert private(f"O:BAG:SYD:P(D;OICI;FA;;;BU)(A;OICI;FA;;;{OWNER})")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "garbage",
            "O:BAG:SY",  # no DACL
            "O:BAG:SYD:NO_ACCESS_CONTROL",  # a missing list lets everyone in
            f"O:BAG:SYD:P(A;OICI;FA;;{OWNER})",  # five fields
            f"O:BAG:SYD:P(A;OICI;FA;;;{OWNER})junk",
            f"O:BAG:SYD:P(A;OICI;FA;;;{OWNER}",  # unbalanced
            'O:BAG:SYD:P(XA;;FX;;;WD;(@User.Title=="x"))',  # nested parentheses
        ],
    )
    def test_anything_it_cannot_read_is_not_private(self, text: str) -> None:
        assert not private(text)

    @pytest.mark.parametrize(
        "principal",
        [
            OTHER_USER,  # a second account, granted by name
            "S-1-5-21-111-222-333-513",  # a domain group
            "S-1-5-19",  # Local Service
            "S-1-5-32-547",  # Power Users
            "LA",
            "DA",
            "RC",
            "S-1-5-80-123-456-789-1011-1213",  # a service account
        ],
    )
    def test_a_trustee_that_is_not_the_owner_or_a_system_principal_is_not_private(
        self, principal: str
    ) -> None:
        assert not private(f"{PRIVATE}(A;OICI;FA;;;{principal})")

    def test_the_owner_is_matched_whatever_the_case(self) -> None:
        assert private(f"O:BAG:SYD:P(A;OICI;FA;;;{OWNER.lower()})")

    def test_another_owner_is_not_the_owner(self) -> None:
        assert not sddl_is_private(PRIVATE, OTHER_USER)

    @pytest.mark.parametrize("user_sid", ["", "BA", "S-1-5-21-1\n", " S-1-5-21-1", "bob"])
    def test_an_unusable_owner_sid_is_never_private(self, user_sid: str) -> None:
        assert not sddl_is_private(PRIVATE, user_sid)

    @pytest.mark.parametrize("kind", ["AU", "AL", "ML", "XU", "RA", "SP", "??", ""])
    def test_an_entry_of_an_unknown_kind_in_the_list_is_not_private(self, kind: str) -> None:
        assert not private(f"{PRIVATE}({kind};;FA;;;{OWNER})")

    @pytest.mark.parametrize("kind", ["D", "OD", "XD"])
    def test_deny_kinds_are_ignored(self, kind: str) -> None:
        assert private(f"{PRIVATE}({kind};;FA;;;{OTHER_USER})")

    def test_audit_entries_after_the_access_list_are_ignored(self) -> None:
        assert private(f"{PRIVATE}S:AI(AU;SA;FA;;;WD)")

    def test_the_owner_or_group_are_not_mistaken_for_the_list(self) -> None:
        # Aliases that end in D sit right before ``D:`` in a real SDDL string.
        assert private(f"O:RDG:EDD:PAI(A;OICI;FA;;;{OWNER})")


class TestParseAccessList:
    def test_the_user_and_the_list_are_split(self) -> None:
        assert parse_access_list(f"{OWNER}\r\n{PRIVATE}\r\n") == AccessList(OWNER, PRIVATE)

    @pytest.mark.parametrize(
        "output",
        ["", f"{OWNER}\n", PRIVATE, f"{PRIVATE}\n{OWNER}", f"{OWNER}\n{PRIVATE}\nextra", "x\ny"],
    )
    def test_anything_else_is_an_error(self, output: str) -> None:
        with pytest.raises(VaultAccessError, match="does not understand"):
            parse_access_list(output)

    def test_is_private_uses_the_reported_user(self) -> None:
        assert is_private(AccessList(OWNER, PRIVATE))
        assert not is_private(AccessList(OTHER_USER, PRIVATE))


class FakeFolder:
    """Stands in for PowerShell: a folder whose list is read and replaced in memory."""

    def __init__(self, sddl: str, *, fix: bool = True) -> None:
        self.sddl = sddl
        self.fix = fix
        self.hardened: list[Path] = []

    def read(self, _folder: Path) -> AccessList:
        return AccessList(OWNER, self.sddl)

    def harden(self, folder: Path) -> None:
        self.hardened.append(folder)
        if self.fix:
            self.sddl = PRIVATE


class TestSecureVaultFolder:
    def secure(self, root: Path, fake: FakeFolder, *, created: bool) -> None:
        secure_vault_folder(root, created=created, read=fake.read, harden=fake.harden)

    def test_a_new_folder_is_hardened_and_checked(self, tmp_path: Path) -> None:
        fake = FakeFolder(OPEN)
        self.secure(tmp_path, fake, created=True)
        assert fake.hardened == [tmp_path]

    def test_a_new_folder_that_is_still_open_after_hardening_is_refused(
        self, tmp_path: Path
    ) -> None:
        fake = FakeFolder(OPEN, fix=False)
        with pytest.raises(VaultAccessError, match="still open"):
            self.secure(tmp_path, fake, created=True)

    def test_an_existing_private_folder_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / "vault.db").write_bytes(b"data")
        fake = FakeFolder(PRIVATE)
        self.secure(tmp_path, fake, created=False)
        assert fake.hardened == []

    def test_an_existing_empty_open_folder_is_hardened(self, tmp_path: Path) -> None:
        fake = FakeFolder(OPEN)
        self.secure(tmp_path, fake, created=False)
        assert fake.hardened == [tmp_path]

    def test_an_existing_open_folder_with_files_is_refused_and_unchanged(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "notes.txt").write_text("someone else's file", encoding="utf-8")
        fake = FakeFolder(OPEN)
        with pytest.raises(VaultAccessError, match="already has files"):
            self.secure(tmp_path, fake, created=False)
        assert fake.hardened == []

    def test_an_existing_folder_shared_with_a_named_second_account_is_refused(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "vault.db").write_bytes(b"data")
        fake = FakeFolder(f"{PRIVATE}(A;OICI;FA;;;{OTHER_USER})")
        with pytest.raises(VaultAccessError, match="already has files"):
            self.secure(tmp_path, fake, created=False)
        assert fake.hardened == []

    def test_a_list_that_cannot_be_read_is_not_trusted(self, tmp_path: Path) -> None:
        def broken(_folder: Path) -> AccessList:
            raise VaultAccessError("no PowerShell")

        with pytest.raises(VaultAccessError):
            secure_vault_folder(
                tmp_path, created=False, read=broken, harden=FakeFolder(OPEN).harden
            )


class TestEnsureVaultOnWindows:
    """``ensure_vault`` calls the check before it creates anything inside the folder."""

    def test_the_folder_is_secured_before_the_subfolders_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[bool, list[str]]] = []

        def fake_secure(root: Path, *, created: bool) -> None:
            seen.append((created, sorted(p.name for p in root.iterdir())))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", fake_secure)
        layout.ensure_vault(tmp_path / "AIVault")
        assert seen == [(True, [])]  # new, and still empty when it was secured
        layout.ensure_vault(tmp_path / "AIVault")
        assert seen[1][0] is False  # the second start finds it existing

    def test_a_refusal_stops_the_start_before_the_subfolders_are_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_root: Path, *, created: bool) -> None:
            raise VaultAccessError("open to other accounts")

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", refuse)
        with pytest.raises(layout.VaultPathError, match="open to other accounts"):
            layout.ensure_vault(tmp_path / "AIVault")
        assert not (tmp_path / "AIVault" / "blobs").exists()

    def test_other_systems_do_not_call_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def never(_root: Path, *, created: bool) -> None:
            raise AssertionError("must not run")

        monkeypatch.setattr(acl, "IS_WINDOWS", False)
        monkeypatch.setattr(acl, "secure_vault_folder", never)
        assert layout.ensure_vault(tmp_path / "AIVault").blobs.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="uses PowerShell and Windows access lists")
class TestRealWindowsAccessLists:  # pragma: no cover - runs on the Windows CI job only
    def grant(self, folder: Path, sid: str) -> None:
        # A leading * means "this SID", whatever language Windows is in.
        subprocess.run(  # noqa: S603
            ["icacls", str(folder), "/grant", f"*{sid}:(OI)(CI)R"],  # noqa: S607
            check=True,
            capture_output=True,
        )

    def grant_users(self, folder: Path) -> None:
        self.grant(folder, "S-1-5-32-545")  # Builtin Users

    def is_private(self, folder: Path) -> bool:
        return is_private(acl.read_access(folder))

    def test_a_new_vault_folder_is_private(self, tmp_path: Path) -> None:
        paths = layout.ensure_vault(tmp_path / "AIVault")
        assert self.is_private(paths.root)
        assert self.is_private(paths.blobs)  # inherited by the subfolders

    def test_an_empty_folder_that_lets_users_in_is_hardened(self, tmp_path: Path) -> None:
        folder = tmp_path / "AIVault"
        folder.mkdir()
        self.grant_users(folder)
        assert not self.is_private(folder)
        layout.ensure_vault(folder)
        assert self.is_private(folder)

    def test_a_folder_with_files_that_lets_users_in_is_refused(self, tmp_path: Path) -> None:
        folder = tmp_path / "AIVault"
        folder.mkdir()
        (folder / "note.txt").write_text("x", encoding="utf-8")
        self.grant_users(folder)
        with pytest.raises(layout.VaultPathError, match="already has files"):
            layout.ensure_vault(folder)
        assert not (folder / "blobs").exists()
        assert not self.is_private(folder)

    def test_a_folder_shared_with_one_named_account_is_not_private(self, tmp_path: Path) -> None:
        # Local Service is no broad group; it stands for any second account named in the list.
        folder = tmp_path / "AIVault"
        folder.mkdir()
        self.grant(folder, "S-1-5-19")
        assert not self.is_private(folder)
        layout.ensure_vault(folder)  # empty, so it is hardened
        assert self.is_private(folder)
