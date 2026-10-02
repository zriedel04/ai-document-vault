r"""Keep the vault folder private to the person who owns it, on Windows.

``os.chmod(0o600)`` does not set a Windows access list: a folder under ``C:\`` inherits an entry
that lets every signed-in account on the PC read what is inside (and ``D:\`` often does the same).
The local token in the vault would then let another Windows account call the API. So on Windows
:func:`secure_vault_folder` removes the inherited entries and grants only the current user, then
reads the access list back and refuses to go on if anyone else can still get in.

The access list is read and written through PowerShell, in the language-independent SDDL form, so
nothing depends on the language of Windows. Any failure to read it counts as "not private".

The check is an allow-list: every entry that grants access must name the current user or one of a
few system principals, and the owner (who can rewrite the list) must be the current user or one of
them. A list of "bad" groups would miss a second account or a domain group that was granted access
by name, which is just as able to read the token and the documents. The same check is made on the
files and folders of an existing vault (the token, the databases and their side files, and then
everything below the vault folder, without following links), because a file moved in from
elsewhere keeps its own access list instead of taking the folder's, and any account may open a
file whose path it knows even when the folders above it are private.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, NamedTuple

IS_WINDOWS: Final = os.name == "nt"

# The only trustees besides the current user that an entry granting access may name: SYSTEM,
# Builtin Administrators (an administrator can take ownership of any folder, so naming the group
# gives nothing away) and the Creator/Owner placeholders. SDDL aliases first, then the SIDs.
PERMITTED_PRINCIPALS: Final = frozenset(
    {"SY", "BA", "CO", "OW", "S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4"}
)
ALLOW_TYPES: Final = frozenset({"A", "OA", "XA", "ZA"})
DENY_TYPES: Final = frozenset({"D", "OD", "XD"})
SID = re.compile(r"S-1-\d+(?:-\d+)+")
# The only abbreviations SDDL has for a single user account: the built-in Administrator and Guest.
USER_ALIASES: Final = frozenset({"LA", "LG"})
# Who may own a path besides the current user: SYSTEM and Builtin Administrators. An elevated
# administrator's new folder is owned by the Administrators group, not by the user.
OWNER_PRINCIPALS: Final = frozenset({"SY", "BA", "S-1-5-18", "S-1-5-32-544"})
ACE = re.compile(r"\(([^()]*)\)")
POWERSHELL_TIMEOUT_SECONDS: Final = 60
# Reading one access list per file takes a while in a large vault, so the walk gets much longer.
TREE_TIMEOUT_SECONDS: Final = 15 * 60
TREE_EXAMPLES: Final = 3
EXAMPLES_SHOWN: Final = 5

# The folder arrives in an environment variable, never in the script text, so no path can be
# read as PowerShell code. The scripts use .NET directly instead of Get-Acl and Set-Acl: those
# live in a module that Windows PowerShell cannot load when it was started from PowerShell 7
# (its PSModulePath points at the wrong copy).
READ_SCRIPT: Final = (
    "$ErrorActionPreference='Stop';"
    "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
    # The same SID as SDDL writes it: the built-in Administrator account (RID 500) shows up as the
    # alias "LA", so the access list cannot be compared with the SID alone.
    "$form=(New-Object System.Security.AccessControl.RawSecurityDescriptor('O:'+$sid))"
    ".GetSddlForm('Owner');"
    # A tab ([char]9) separates the fields; no double quotes, which are fragile on a command line.
    "$sid+[char]9+$form;"
    # One line per path: the owner, then the access list. A file and a folder need different
    # classes to read their security descriptor.
    "foreach($p in ($env:VAULT_PATHS -split '\\|')){"
    "if([System.IO.Directory]::Exists($p)){"
    "$acl=(New-Object System.IO.DirectoryInfo($p)).GetAccessControl()}"
    "else{$acl=(New-Object System.IO.FileInfo($p)).GetAccessControl()};"
    "$acl.GetSecurityDescriptorSddlForm('Owner')+[char]9+"
    "$acl.GetSecurityDescriptorSddlForm('Access')}"
)
# Every file and folder below the vault folder (links are listed but not followed), grouped by
# owner and access list: one line per distinct list with how many entries have it and a few example
# paths relative to the vault folder. A vault has a handful of distinct lists, so the output stays
# small however many files there are, and the program judges each list once.
TREE_SCRIPT: Final = (
    "$ErrorActionPreference='Stop';"
    "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
    "$form=(New-Object System.Security.AccessControl.RawSecurityDescriptor('O:'+$sid))"
    ".GetSddlForm('Owner');"
    "$sid+[char]9+$form;"
    "$root=$env:VAULT_FOLDER.TrimEnd('\\');"
    "$count=@{};$examples=@{};"
    "$stack=New-Object System.Collections.Stack;$stack.Push($root);"
    "while($stack.Count -gt 0){"
    "$dir=$stack.Pop();"
    "foreach($e in (New-Object System.IO.DirectoryInfo($dir)).EnumerateFileSystemInfos()){"
    "$acl=$e.GetAccessControl();"
    "$key=$acl.GetSecurityDescriptorSddlForm('Owner')+[char]9+"
    "$acl.GetSecurityDescriptorSddlForm('Access');"
    "if(-not $count.ContainsKey($key)){$count[$key]=0;"
    "$examples[$key]=New-Object System.Collections.ArrayList};"
    "$count[$key]=$count[$key]+1;"
    f"if($examples[$key].Count -lt {TREE_EXAMPLES}){{"
    "[void]$examples[$key].Add($e.FullName.Substring($root.Length+1))};"
    "if(($e -is [System.IO.DirectoryInfo]) -and "
    "-not ($e.Attributes -band [System.IO.FileAttributes]::ReparsePoint)){"
    "$stack.Push($e.FullName)}}};"
    "foreach($key in $count.Keys){"
    "[string]$count[$key]+[char]9+$key+[char]9+($examples[$key] -join '|')}"
)
MAKE_PRIVATE_SCRIPT: Final = (
    "$ErrorActionPreference='Stop';"
    "$folder=New-Object System.IO.DirectoryInfo($env:VAULT_FOLDER);"
    "$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;"
    "$acl=$folder.GetAccessControl();"
    "$acl.SetAccessRuleProtection($true,$false);"
    "foreach($rule in @($acl.Access)){[void]$acl.RemoveAccessRule($rule)};"
    "$grant=New-Object System.Security.AccessControl.FileSystemAccessRule("
    "$user,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
    "$acl.AddAccessRule($grant);"
    "$folder.SetAccessControl($acl)"
)


class VaultAccessError(RuntimeError):
    """The vault folder is open to other accounts, or its access list could not be checked."""


DACL_START = re.compile(r"D:(?=(?:P|AI|AR|NO_ACCESS_CONTROL|\())")
SACL_START = re.compile(r"S:(?=(?:P|AI|AR|NO_ACCESS_CONTROL|\())")
# Flags, then entries made only of simple (not nested) parentheses.
DACL_BODY = re.compile(r"(?:P|AI|AR)*((?:\([^()]*\))*)")


class AccessList(NamedTuple):
    """What PowerShell reported about one path: the current user's SID, the way SDDL writes that
    SID, the path's owner (SDDL form) and its access list (SDDL)."""

    user_sid: str
    user_alias: str
    owner: str
    sddl: str


OWNER_FORM = re.compile(r"O:([A-Z]{2}|S-1-\d+(?:-\d+)+)")
BAD_OUTPUT = "PowerShell returned an access list this program does not understand"


def _user_line(line: str) -> tuple[str, str]:
    """The current user's SID and the way SDDL writes it, from the first line of the output."""
    user_sid, _, form = line.partition("\t")
    user = OWNER_FORM.fullmatch(form)
    if SID.fullmatch(user_sid) is None or user is None:
        raise VaultAccessError(BAD_OUTPUT)
    alias = user.group(1)
    if alias != user_sid and alias not in USER_ALIASES:
        raise VaultAccessError(BAD_OUTPUT)
    return user_sid, alias


def parse_access_lists(output: str, count: int) -> list[AccessList]:
    """Split what the read script prints for ``count`` paths. Anything else is an error."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != count + 1:
        raise VaultAccessError(BAD_OUTPUT)
    user_sid, alias = _user_line(lines[0])
    result = []
    for line in lines[1:]:
        owner_form, tab, sddl = line.partition("\t")
        owner = OWNER_FORM.fullmatch(owner_form)
        if not tab or owner is None:
            raise VaultAccessError(BAD_OUTPUT)
        result.append(AccessList(user_sid, alias, owner.group(1), sddl))
    return result


class TreeGroup(NamedTuple):
    """Entries below the vault folder that share one owner and access list."""

    access: AccessList
    entries: int  # how many files and folders have this owner and list
    examples: tuple[str, ...]  # paths relative to the vault folder


def parse_tree(output: str) -> list[TreeGroup]:
    """Split what the tree script prints: the user line, then one line per distinct list."""
    lines = [line.strip("\r\n") for line in output.splitlines() if line.strip()]
    if not lines:
        raise VaultAccessError(BAD_OUTPUT)
    user_sid, alias = _user_line(lines[0].strip())
    groups = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 4 or not fields[0].isdecimal() or int(fields[0]) < 1:
            raise VaultAccessError(BAD_OUTPUT)
        owner = OWNER_FORM.fullmatch(fields[1])
        if owner is None:
            raise VaultAccessError(BAD_OUTPUT)
        examples = tuple(name for name in fields[3].split("|") if name)
        access = AccessList(user_sid, alias, owner.group(1), fields[2])
        groups.append(TreeGroup(access, int(fields[0]), examples))
    return groups


def parse_access_list(output: str) -> AccessList:
    return parse_access_lists(output, 1)[0]


def is_private(access: AccessList) -> bool:
    """True if the list is private and the owner, who can rewrite it, is trusted too."""
    user_names = {access.user_sid.upper(), access.user_alias.upper()}
    owner = access.owner.upper()
    if owner not in user_names and owner not in OWNER_PRINCIPALS:
        return False
    return sddl_is_private(access.sddl, access.user_sid, access.user_alias)


def sddl_is_private(sddl: str, user_sid: str, user_alias: str | None = None) -> bool:
    """True if every entry that grants access names the user or a permitted principal.

    The user is ``user_sid`` or, where SDDL abbreviates that SID (the built-in Administrator
    account is ``LA``), ``user_alias``.

    Fails closed. Anything that is not a plain, readable DACL is not private: no ``D:`` part, a
    "no access control" (everyone allowed) list, an entry with fewer than six fields or of a type
    this reader does not know, or text it does not understand. Deny entries are ignored: they can
    only take access away.
    """
    if SID.fullmatch(user_sid) is None:
        return False
    user_names = {user_sid.upper()}
    if user_alias is not None and user_alias.upper() in USER_ALIASES:
        user_names.add(user_alias.upper())
    text = sddl.strip()  # PowerShell ends its output with a line break
    start = DACL_START.search(text)
    if start is None:
        return False
    dacl = text[start.end() :]
    sacl = SACL_START.search(dacl)
    if sacl is not None:
        dacl = dacl[: sacl.start()]
    body = DACL_BODY.fullmatch(dacl)
    if body is None:
        return False
    for entry in ACE.findall(body.group(1)):
        fields = entry.split(";")
        if len(fields) < 6:
            return False
        ace_type, principal = fields[0], fields[5].upper()
        if ace_type in DENY_TYPES:
            continue
        if ace_type not in ALLOW_TYPES:
            return False
        if principal not in user_names and principal not in PERMITTED_PRINCIPALS:
            return False
    return True


def _powershell() -> str:  # pragma: no cover - Windows only
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _run(
    script: str, *paths: Path, timeout: int = POWERSHELL_TIMEOUT_SECONDS
) -> str:  # pragma: no cover - Windows only
    command = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script]
    environment = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    # ``|`` cannot be part of a Windows path, so it separates them safely.
    environment["VAULT_FOLDER"] = str(paths[0])
    environment["VAULT_PATHS"] = "|".join(str(path) for path in paths)
    try:
        result = subprocess.run(  # noqa: S603 - fixed program path, fixed script, no user input
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        msg = "could not run PowerShell to check the vault folder's permissions"
        raise VaultAccessError(msg) from error
    if result.returncode != 0:
        # PowerShell's own message names the cmdlet and the folder, never file contents.
        detail = " ".join(result.stderr.split())[:300]
        msg = f"PowerShell could not read or change the vault folder's permissions: {detail}"
        raise VaultAccessError(msg)
    return result.stdout


def read_accesses(paths: Sequence[Path]) -> list[AccessList]:  # pragma: no cover - Windows only
    return parse_access_lists(_run(READ_SCRIPT, *paths), len(paths))


def read_access(folder: Path) -> AccessList:  # pragma: no cover - Windows only
    return read_accesses([folder])[0]


def read_tree(root: Path) -> list[TreeGroup]:  # pragma: no cover - Windows only
    return parse_tree(_run(TREE_SCRIPT, root, timeout=TREE_TIMEOUT_SECONDS))


def make_private(folder: Path) -> None:  # pragma: no cover - Windows only
    _run(MAKE_PRIVATE_SCRIPT, folder)


def secure_vault_folder(
    root: Path,
    *,
    created: bool,
    read: Callable[[Path], AccessList] = read_access,
    harden: Callable[[Path], None] = make_private,
) -> None:
    """Make ``root`` private to its owner and prove it, or raise :class:`VaultAccessError`.

    A folder this program just created, or an existing one with nothing in it, is hardened. A
    folder that already holds files is only checked: changing who can open someone else's folder
    is not this program's decision, so the error tells the owner what to do.
    """
    if not created and is_private(read(root)):
        return
    if created or not any(root.iterdir()):
        harden(root)
        after = read(root)
        if is_private(after):
            return
        # The access list is SIDs and rights only, never file contents, and tells the owner (or a
        # bug report) exactly who is still allowed in.
        msg = (
            f"the vault folder {root} is still open to other accounts after setting its "
            f"permissions (owner: {after.owner}; access list: {after.sddl[:300]}; "
            f"your account: {after.user_sid})"
        )
        raise VaultAccessError(msg)
    msg = (
        f"the vault folder {root} can be read by other accounts on this PC, and it already has "
        "files in it, so it was not changed. Remove the extra users from its Security tab (turn "
        "off inheritance and keep only your account), or choose an empty folder"
    )
    raise VaultAccessError(msg)


def verify_private(
    paths: Sequence[Path],
    *,
    read: Callable[[Sequence[Path]], list[AccessList]] = read_accesses,
) -> None:
    """Refuse to go on if any of these existing files or folders can be used by other accounts.

    Nothing is changed: a file that is open is either one the owner moved in from elsewhere or
    one that may already have been read, so the owner decides (for the token, that means deleting
    it so a new one is made). Only the names are reported, never what is inside.
    """
    if not paths:
        return
    accesses = read(paths)
    if len(accesses) != len(paths):
        raise VaultAccessError(BAD_OUTPUT)
    open_names = [
        path.name for path, access in zip(paths, accesses, strict=True) if not is_private(access)
    ]
    if open_names:
        msg = (
            "these items in the vault folder can be used by other accounts on this PC: "
            + ", ".join(open_names)
            + ". They were not changed. Turn off inheritance and keep only your account in each "
            "one's Security tab, or move them out and copy them back in (a copy takes the "
            "folder's permissions); if it is `local-token`, delete it so a new one is made"
        )
        raise VaultAccessError(msg)


def verify_tree(
    root: Path,
    *,
    read: Callable[[Path], list[TreeGroup]] = read_tree,
) -> None:
    """Refuse to go on if anything below ``root`` can be used by other accounts.

    A file whose full path is known can be opened by any account that has its own entry in the
    file's access list, whatever the folders above say (Windows lets ordinary accounts traverse
    folders they cannot list), so a tree moved in from elsewhere has to be checked file by file.
    Nothing is changed. Only paths relative to the vault folder are reported, never content.
    """
    open_groups = [group for group in read(root) if not is_private(group.access)]
    if not open_groups:
        return
    total = sum(group.entries for group in open_groups)
    examples = [name for group in open_groups for name in group.examples][:EXAMPLES_SHOWN]
    msg = (
        f"{total} files or folders below the vault folder can be used by other accounts on this "
        f"PC, for example: {', '.join(examples)}. They were not changed. Turn off inheritance "
        "and keep only your account in their Security tab, or move them out and copy them back "
        "in (a copy takes the folder's permissions)"
    )
    raise VaultAccessError(msg)
