r"""Keep the vault folder private to the person who owns it, on Windows.

``os.chmod(0o600)`` does not set a Windows access list: a folder under ``C:\`` inherits an entry
that lets every signed-in account on the PC read what is inside (and ``D:\`` often does the same).
The local token in the vault would then let another Windows account call the API. So on Windows
:func:`secure_vault_folder` removes the inherited entries and grants only the current user, then
reads the access list back and refuses to go on if anyone else can still get in.

The access list is read and written through PowerShell, in the language-independent SDDL form, so
nothing depends on the language of Windows. Any failure to read it counts as "not private".

The check is an allow-list: every entry that grants access must name the current user or one of a
few system principals. A list of "bad" groups would miss a second account or a domain group that
was granted access by name, which is just as able to read the token and the documents.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
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
ACE = re.compile(r"\(([^()]*)\)")
POWERSHELL_TIMEOUT_SECONDS: Final = 60

# The folder arrives in an environment variable, never in the script text, so no path can be
# read as PowerShell code. The scripts use .NET directly instead of Get-Acl and Set-Acl: those
# live in a module that Windows PowerShell cannot load when it was started from PowerShell 7
# (its PSModulePath points at the wrong copy).
READ_SCRIPT: Final = (
    "$ErrorActionPreference='Stop';"
    "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
    "(New-Object System.IO.DirectoryInfo($env:VAULT_FOLDER)).GetAccessControl()"
    ".GetSecurityDescriptorSddlForm('Access')"
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
    """What PowerShell reported: the current user's SID and the folder's access list (SDDL)."""

    user_sid: str
    sddl: str


def parse_access_list(output: str) -> AccessList:
    """Split the two lines the read script prints. Anything else is an error, never a guess."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 2 or SID.fullmatch(lines[0]) is None:
        msg = "PowerShell returned an access list this program does not understand"
        raise VaultAccessError(msg)
    return AccessList(user_sid=lines[0], sddl=lines[1])


def is_private(access: AccessList) -> bool:
    return sddl_is_private(access.sddl, access.user_sid)


def sddl_is_private(sddl: str, user_sid: str) -> bool:
    """True if every entry that grants access names ``user_sid`` or a permitted principal.

    Fails closed. Anything that is not a plain, readable DACL is not private: no ``D:`` part, a
    "no access control" (everyone allowed) list, an entry with fewer than six fields or of a type
    this reader does not know, or text it does not understand. Deny entries are ignored: they can
    only take access away.
    """
    if SID.fullmatch(user_sid) is None:
        return False
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
        if principal != user_sid.upper() and principal not in PERMITTED_PRINCIPALS:
            return False
    return True


def _powershell() -> str:  # pragma: no cover - Windows only
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _run(script: str, folder: Path) -> str:  # pragma: no cover - Windows only
    command = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script]
    environment = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    environment["VAULT_FOLDER"] = str(folder)
    try:
        result = subprocess.run(  # noqa: S603 - fixed program path, fixed script, no user input
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=POWERSHELL_TIMEOUT_SECONDS,
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


def read_access(folder: Path) -> AccessList:  # pragma: no cover - Windows only
    return parse_access_list(_run(READ_SCRIPT, folder))


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
        if is_private(read(root)):
            return
        msg = (
            f"the vault folder {root} is still open to other accounts after setting its permissions"
        )
        raise VaultAccessError(msg)
    msg = (
        f"the vault folder {root} can be read by other accounts on this PC, and it already has "
        "files in it, so it was not changed. Remove the extra users from its Security tab (turn "
        "off inheritance and keep only your account), or choose an empty folder"
    )
    raise VaultAccessError(msg)
