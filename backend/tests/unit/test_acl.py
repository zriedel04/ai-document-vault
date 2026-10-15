"""The Windows access-list rules, tested through the pure parts and with fakes.

The calls to PowerShell run only on Windows (the Windows CI job covers them at the end of this
file); everything that decides whether a folder is "private" is plain text parsing and runs on
every system.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from vault.storage import acl, layout
from vault.storage.acl import (
    AccessList,
    TreeGroup,
    VaultAccessError,
    is_private,
    parse_access_list,
    parse_access_lists,
    parse_tree,
    sddl_is_private,
    secure_vault_folder,
    verify_private,
    verify_tree,
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


def access(
    sddl: str = PRIVATE, *, owner: str = OWNER, user: str = OWNER, alias: str | None = None
) -> AccessList:
    return AccessList(user, alias or user, owner, sddl)


def line(owner: str = OWNER, sddl: str = PRIVATE) -> str:
    return f"O:{owner}\t{sddl}"


HEADER = f"{OWNER}\tO:{OWNER}"


class TestParseAccessList:
    def test_the_user_the_owner_and_the_list_are_split(self) -> None:
        output = f"{HEADER}\r\n{line()}\r\n"
        assert parse_access_list(output) == access()

    def test_several_paths_come_back_in_order(self) -> None:
        output = f"{HEADER}\n{line('BA')}\n{line(OWNER, 'D:P(A;;FA;;;SY)')}\n"
        first, second = parse_access_lists(output, 2)
        assert (first.owner, second.owner) == ("BA", OWNER)
        assert second.sddl == "D:P(A;;FA;;;SY)"
        assert first.user_sid == second.user_sid == OWNER

    def test_the_built_in_administrator_is_reported_by_its_alias(self) -> None:
        parsed = parse_access_list(f"{OWNER}\tO:LA\n{line()}\n")
        assert parsed.user_alias == "LA"

    @pytest.mark.parametrize(
        "output",
        [
            "",
            HEADER,  # no path line
            f"{HEADER}\n",
            line(),  # no header
            f"{HEADER}\n{line()}\nextra",
            f"{HEADER}\n{line()}\n{line()}",  # one path was asked for
            f"{OWNER}\n{line()}",  # the header has no alias
            f"{OWNER}\tO:WD\n{line()}",  # Everyone is not an alias for one user
            f"{OWNER}\tO:BA\n{line()}",
            f"{OWNER}\tO:{OTHER_USER}\n{line()}",  # not the same account
            f"{OWNER}\towner LA\n{line()}",
            f"{HEADER}\n{OWNER}\t{PRIVATE}",  # an owner that is not in SDDL form
            f"{HEADER}\nO:{OWNER} {PRIVATE}",  # no tab between owner and list
            f"{HEADER}\nO:\t{PRIVATE}",
            "x\ny\nz",
        ],
    )
    def test_anything_else_is_an_error(self, output: str) -> None:
        with pytest.raises(VaultAccessError, match="does not understand"):
            parse_access_list(output)

    def test_is_private_uses_the_reported_user(self) -> None:
        assert is_private(access())
        assert not is_private(access(user=OTHER_USER, owner=OTHER_USER))
        assert not is_private(access(user=OTHER_USER))  # the list names a different account

    def test_the_built_in_administrator_alias_in_the_list_is_the_user(self) -> None:
        sddl = "O:BAG:SYD:PAI(A;OICI;FA;;;LA)"  # what the Windows runner reports for its account
        assert is_private(access(sddl, owner="BA", alias="LA"))

    def test_the_alias_is_the_user_only_when_it_was_reported_for_the_user(self) -> None:
        sddl = "O:BAG:SYD:PAI(A;OICI;FA;;;LA)"
        assert not is_private(access(sddl, owner="BA"))  # another account's alias
        assert not sddl_is_private(sddl, OWNER)

    @pytest.mark.parametrize("alias", ["WD", "BU", "AU", "IU", "AN"])
    def test_a_broad_group_is_never_taken_for_the_user(self, alias: str) -> None:
        assert not sddl_is_private(f"O:BAG:SYD:PAI(A;OICI;FA;;;{alias})", OWNER, alias)


class TestOwner:
    """Whoever owns a path can rewrite its list and, through Owner Rights, may be granted access."""

    @pytest.mark.parametrize("owner", [OWNER, "BA", "SY", "S-1-5-32-544", "S-1-5-18"])
    def test_the_user_and_the_system_principals_may_own_it(self, owner: str) -> None:
        assert is_private(access(owner=owner))

    def test_the_built_in_administrator_alias_may_own_it(self) -> None:
        assert is_private(access("D:P(A;;FA;;;LA)", owner="LA", alias="LA"))

    @pytest.mark.parametrize(
        "owner", [OTHER_USER, "LA", "DA", "WD", "BU", "AU", "OW", "CO", "S-1-5-21-9-9-9-513"]
    )
    def test_another_owner_is_not_private_even_if_the_list_looks_right(self, owner: str) -> None:
        assert not is_private(access(owner=owner))

    def test_an_owner_rights_entry_does_not_make_another_owner_acceptable(self) -> None:
        # The case from review: the user and Owner Rights are allowed, but a second account owns it.
        sddl = f"O:BAG:SYD:PAI(A;OICI;FA;;;{OWNER})(A;OICI;FA;;;OW)"
        assert not is_private(access(sddl, owner=OTHER_USER))
        assert is_private(access(sddl, owner=OWNER))


class TestVerifyPrivate:
    def paths(self, *names: str) -> list[Path]:
        return [Path("vault") / name for name in names]

    def test_nothing_to_check_asks_nothing(self) -> None:
        def never(_paths: object) -> list[AccessList]:
            raise AssertionError("must not run")

        verify_private([], read=never)

    def test_private_entries_pass(self) -> None:
        verify_private(self.paths("a", "b"), read=lambda p: [access() for _ in p])

    def test_every_open_entry_is_named_and_nothing_else(self) -> None:
        opened = {"local-token", "vault.db"}

        def read(paths: Sequence[Path]) -> list[AccessList]:
            return [access(OPEN) if p.name in opened else access() for p in paths]

        with pytest.raises(VaultAccessError) as caught:
            verify_private(self.paths("local-token", "blobs", "vault.db"), read=read)
        message = str(caught.value)
        assert "local-token, vault.db" in message
        assert "blobs" not in message
        assert "delete it" in message
        assert "S-1-5" not in message  # no access list in this message

    def test_an_entry_with_another_owner_is_named(self) -> None:
        with pytest.raises(VaultAccessError, match=r"vault\.db"):
            verify_private(self.paths("vault.db"), read=lambda p: [access(owner=OTHER_USER)])

    def test_a_reading_that_does_not_match_the_paths_is_an_error(self) -> None:
        with pytest.raises(VaultAccessError, match="does not understand"):
            verify_private(self.paths("a", "b"), read=lambda p: [access()])

    def test_a_reading_that_fails_is_not_trusted(self) -> None:
        def broken(_paths: object) -> list[AccessList]:
            raise VaultAccessError("no PowerShell")

        with pytest.raises(VaultAccessError):
            verify_private(self.paths("a"), read=broken)


def group(count: int, *examples: str, sddl: str = PRIVATE, owner: str = OWNER) -> TreeGroup:
    return TreeGroup(access(sddl, owner=owner), count, examples)


def tree_line(
    count: int | str, owner: str = OWNER, sddl: str = PRIVATE, examples: str = "a"
) -> str:
    return f"{count}\t{owner and 'O:' + owner}\t{sddl}\t{examples}"


class TestParseTree:
    def test_an_empty_vault_is_just_the_user_line(self) -> None:
        assert parse_tree(f"{HEADER}\r\n") == []

    def test_one_group_per_distinct_access_list(self) -> None:
        output = (
            f"{HEADER}\r\n"
            f"{tree_line(12000, examples='blobs\\a|blobs\\b|derived\\c')}\r\n"
            f"{tree_line(2, owner='BA', sddl=OPEN, examples='moved.pdf')}\r\n"
        )
        first, second = parse_tree(output)
        assert (first.entries, first.examples) == (12000, ("blobs\\a", "blobs\\b", "derived\\c"))
        assert first.access == access(PRIVATE)
        assert (second.entries, second.access.owner, second.access.sddl) == (2, "BA", OPEN)

    def test_the_built_in_administrator_alias_is_taken_from_the_user_line(self) -> None:
        parsed = parse_tree(f"{OWNER}\tO:LA\n{tree_line(1, owner='LA')}\n")
        assert parsed[0].access.user_alias == "LA"

    @pytest.mark.parametrize(
        "output",
        [
            "",
            "\n\n",
            "garbage",
            f"{OWNER}\tO:{OWNER}\n3\tO:SY",  # too few fields
            f"{HEADER}\n{tree_line(0)}",  # a group has at least one entry
            f"{HEADER}\n{tree_line(-1)}",
            f"{HEADER}\n{tree_line('x')}",
            f"{HEADER}\n{tree_line('1.5')}",
            f"{HEADER}\n{tree_line(1, owner='nonsense')}",
            f"{HEADER}\n{tree_line(1, owner='')}",
            f"{HEADER}\n{tree_line(1)}\textra",
            f"{OTHER_USER}\tO:{OWNER}\n{tree_line(1)}",  # the user line is not consistent
        ],
    )
    def test_anything_else_is_an_error(self, output: str) -> None:
        with pytest.raises(VaultAccessError, match="does not understand"):
            parse_tree(output)


class TestVerifyTree:
    def test_a_private_tree_passes(self) -> None:
        verify_tree(Path("vault"), read=lambda _root: [group(30000, "blobs\\x"), group(3)])

    def test_an_empty_tree_passes(self) -> None:
        verify_tree(Path("vault"), read=lambda _root: [])

    def test_a_single_open_file_deep_in_the_tree_stops_it(self) -> None:
        deep = "blobs\\sha256\\ab\\cd\\abcd.pdf"
        with pytest.raises(VaultAccessError) as caught:
            verify_tree(
                Path("vault"),
                read=lambda _root: [group(40000, "blobs\\x"), group(1, deep, sddl=OPEN)],
            )
        assert deep in str(caught.value)
        assert "1 files or folders" in str(caught.value)
        assert "blobs\\x" not in str(caught.value)  # the private group is not mentioned

    def test_a_file_with_another_owner_stops_it(self) -> None:
        with pytest.raises(VaultAccessError, match="1 files or folders"):
            verify_tree(Path("vault"), read=lambda _root: [group(1, "a", owner=OTHER_USER)])

    def test_the_count_is_summed_and_the_examples_are_few(self) -> None:
        groups = [
            group(4, "a", "b", "c", sddl=OPEN),
            group(5, "d", "e", "f", owner=OTHER_USER),
        ]
        with pytest.raises(VaultAccessError) as caught:
            verify_tree(Path("vault"), read=lambda _root: groups)
        message = str(caught.value)
        assert "9 files or folders" in message
        assert "a, b, c, d, e" in message
        assert "f" not in message.split("for example:")[1].split(".")[0]

    def test_a_reading_that_fails_is_not_trusted(self) -> None:
        def broken(_root: Path) -> list[TreeGroup]:
            raise VaultAccessError("timed out")

        with pytest.raises(VaultAccessError, match="timed out"):
            verify_tree(Path("vault"), read=broken)


class FakeFolder:
    """Stands in for PowerShell: a folder whose list is read and replaced in memory."""

    def __init__(self, sddl: str, *, fix: bool = True) -> None:
        self.sddl = sddl
        self.fix = fix
        self.hardened: list[Path] = []

    def read(self, _folder: Path) -> AccessList:
        return access(self.sddl)

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
    """``ensure_vault`` calls the checks before it creates anything inside the folder."""

    @pytest.fixture(autouse=True)
    def trees(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """No PowerShell here: record the folders whose whole tree would be walked."""
        walked: list[Path] = []
        monkeypatch.setattr(acl, "verify_tree", walked.append)
        return walked

    @pytest.fixture
    def verified(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """Pretend to be on Windows with nothing to run; record what is verified."""
        seen: list[list[str]] = []
        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda _root, *, created: None)
        monkeypatch.setattr(
            acl, "verify_private", lambda paths: seen.append([p.name for p in paths])
        )
        return seen

    def test_the_folder_is_secured_before_the_subfolders_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[bool, list[str]]] = []

        def fake_secure(root: Path, *, created: bool) -> None:
            seen.append((created, sorted(p.name for p in root.iterdir())))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", fake_secure)
        monkeypatch.setattr(acl, "verify_private", lambda _paths: None)
        layout.ensure_vault(tmp_path / "AIVault")
        assert seen == [(True, [])]  # new, and still empty when it was secured
        layout.ensure_vault(tmp_path / "AIVault")
        assert seen[1][0] is False  # the second start finds it existing

    def test_the_whole_tree_is_walked_after_the_named_entries(
        self, tmp_path: Path, verified: list[list[str]], trees: list[Path]
    ) -> None:
        root = tmp_path / "AIVault"
        layout.ensure_vault(root)
        assert trees == [root]
        layout.ensure_vault(root)
        assert trees == [root, root]  # at every start, not only the first

    def test_an_open_file_deep_in_the_tree_stops_the_start_before_the_subfolders_are_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_root: Path) -> None:
            raise VaultAccessError("1 files or folders below the vault folder: blobs\\x.pdf")

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda _root, *, created: None)
        monkeypatch.setattr(acl, "verify_private", lambda _paths: None)
        monkeypatch.setattr(acl, "verify_tree", refuse)
        root = tmp_path / "AIVault"
        root.mkdir()
        with pytest.raises(layout.VaultPathError, match=r"blobs\\x\.pdf"):
            layout.ensure_vault(root)
        assert not (root / "blobs").exists()

    def test_a_new_vault_has_no_existing_entries_to_verify(
        self, tmp_path: Path, verified: list[list[str]]
    ) -> None:
        layout.ensure_vault(tmp_path / "AIVault")
        assert verified == [[]]

    def test_every_entry_in_the_vault_folder_is_verified_before_the_token_is_read(
        self, tmp_path: Path, verified: list[list[str]]
    ) -> None:
        root = tmp_path / "AIVault"
        for folder in ("blobs", "derived", "logs", "backups"):
            (root / folder).mkdir(parents=True)
        for name in ("local-token", "vault.db", "vault.db-wal", "index.db-shm", "other.txt"):
            (root / name).write_text("x", encoding="utf-8")
        layout.ensure_vault(root)
        assert verified == [
            sorted(
                [
                    "local-token",
                    "vault.db",
                    "vault.db-wal",
                    "index.db-shm",
                    "other.txt",  # anything else in the folder, so a stray file cannot slip by
                    "blobs",
                    "derived",
                    "logs",
                    "backups",
                ]
            )
        ]  # entries that do not exist are not asked for twice

    @pytest.mark.parametrize("database", ["vault.db", "index.db"])
    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_every_sqlite_side_file_of_each_database_is_trusted(
        self, tmp_path: Path, suffix: str, database: str
    ) -> None:
        paths = layout.VaultPaths(tmp_path)
        assert tmp_path / f"{database}{suffix}" in paths.trusted_entries

    @pytest.mark.parametrize(
        "name", ["vault.db-journal", "index.db-journal", "vault.db-mj1A2B3C4D", "copy of vault.db"]
    )
    def test_a_side_file_or_stray_copy_is_verified_even_when_not_named(
        self, tmp_path: Path, verified: list[list[str]], name: str
    ) -> None:
        root = tmp_path / "AIVault"
        root.mkdir()
        (root / name).write_text("x", encoding="utf-8")
        layout.ensure_vault(root)
        assert name in verified[0]

    def test_an_open_existing_entry_stops_the_start_before_the_subfolders_are_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_paths: object) -> None:
            raise VaultAccessError("these items can be used by other accounts: local-token")

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda _root, *, created: None)
        monkeypatch.setattr(acl, "verify_private", refuse)
        root = tmp_path / "AIVault"
        root.mkdir()
        (root / "local-token").write_text("x", encoding="utf-8")
        with pytest.raises(layout.VaultPathError, match="local-token"):
            layout.ensure_vault(root)
        assert not (root / "blobs").exists()

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
        def never(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("must not run")

        monkeypatch.setattr(acl, "IS_WINDOWS", False)
        monkeypatch.setattr(acl, "secure_vault_folder", never)
        monkeypatch.setattr(acl, "verify_private", never)
        assert layout.ensure_vault(tmp_path / "AIVault").blobs.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="uses PowerShell and Windows access lists")
class TestRealWindowsAccessLists:  # pragma: no cover - runs on the Windows CI job only
    def grant(self, path: Path, sid: str, *, flags: str = "(OI)(CI)") -> None:
        # A leading * means "this SID", whatever language Windows is in. Files take no flags.
        subprocess.run(  # noqa: S603
            ["icacls", str(path), "/grant", f"*{sid}:{flags}R"],  # noqa: S607
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

    def test_a_second_start_accepts_the_vault_it_made(self, tmp_path: Path) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        paths.token_file.write_text("x", encoding="utf-8")
        paths.db.write_bytes(b"x")
        layout.ensure_vault(root)  # token, database and subfolders inherited the private list

    def test_a_token_file_with_its_own_open_access_list_stops_the_start(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        paths.token_file.write_text("x", encoding="utf-8")
        self.grant(paths.token_file, "S-1-5-32-545", flags="")  # Users may read the token file
        with pytest.raises(layout.VaultPathError, match="local-token"):
            layout.ensure_vault(root)

    def test_an_open_rollback_journal_stops_the_start(self, tmp_path: Path) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        journal = Path(f"{paths.db}-journal")
        journal.write_bytes(b"x")
        self.grant(journal, "S-1-5-19", flags="")  # one named account, not a broad group
        with pytest.raises(layout.VaultPathError, match=r"vault\.db-journal"):
            layout.ensure_vault(root)

    def test_an_open_file_with_an_unexpected_name_stops_the_start(self, tmp_path: Path) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        stray = paths.root / "vault.db-mj1A2B3C4D"
        stray.write_bytes(b"x")
        self.grant(stray, "S-1-5-32-545", flags="")
        with pytest.raises(layout.VaultPathError, match=r"vault\.db-mj1A2B3C4D"):
            layout.ensure_vault(root)

    def test_a_second_start_accepts_a_tree_of_nested_files_it_made(self, tmp_path: Path) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        for number in range(30):
            folder = paths.blobs / "sha256" / f"{number:02x}" / "cd"
            folder.mkdir(parents=True)
            (folder / f"{number:064x}.pdf").write_bytes(b"x")
        layout.ensure_vault(root)  # every file inherited the private list

    def test_an_open_file_deep_in_the_blobs_stops_the_start(self, tmp_path: Path) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        deep = paths.blobs / "sha256" / "ab" / "cd" / "document.pdf"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b"x")
        self.grant(deep, "S-1-5-32-545", flags="")  # Users may open it by its path
        with pytest.raises(layout.VaultPathError) as caught:
            layout.ensure_vault(root)
        assert "document.pdf" in str(caught.value)
        assert str(tmp_path) not in str(caught.value)  # relative to the vault folder

    def test_a_tree_moved_in_from_elsewhere_keeps_its_lists_and_stops_the_start(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        outside = tmp_path / "old-vault" / "sha256" / "ab"
        outside.mkdir(parents=True)
        moved = outside / "file.pdf"
        moved.write_bytes(b"x")
        self.grant(moved, "S-1-5-19", flags="")  # a named second account
        (tmp_path / "old-vault").rename(paths.blobs / "restored")  # a move keeps each file's list
        with pytest.raises(layout.VaultPathError, match=r"file\.pdf"):
            layout.ensure_vault(root)

    def test_an_open_database_file_stops_the_start(self, tmp_path: Path) -> None:
        root = tmp_path / "AIVault"
        paths = layout.ensure_vault(root)
        paths.db.write_bytes(b"x")
        self.grant(paths.db, "S-1-5-19", flags="")  # one named account, not a broad group
        with pytest.raises(layout.VaultPathError, match=r"vault\.db"):
            layout.ensure_vault(root)


def test_the_launcher_waits_longer_than_the_access_list_walk_may_take() -> None:
    """``vault.ps1 run`` stops a server that has not answered by its deadline. The first start of
    an existing vault walks every file's access list for up to ``TREE_TIMEOUT_SECONDS``, plus the
    other checks (each up to ``POWERSHELL_TIMEOUT_SECONDS``) and the migration, so a shorter
    deadline would stop a healthy server on a large vault every time."""
    launcher = Path(__file__).resolve().parents[3] / "vault.ps1"
    found = re.search(r"^\$script:StartupMinutes = (\d+)\r?$", launcher.read_text("utf-8"), re.M)
    assert found is not None
    wait = int(found.group(1)) * 60
    assert wait >= acl.TREE_TIMEOUT_SECONDS + 4 * acl.POWERSHELL_TIMEOUT_SECONDS
