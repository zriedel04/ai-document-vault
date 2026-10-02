r"""Keep the vault folder private to the person who owns it, on Windows.

``os.chmod(0o600)`` does not set a Windows access list: a folder under ``C:\`` inherits an entry
that lets every signed-in account on the PC read what is inside (and ``D:\`` often does the same).
The local token in the vault would then let another Windows account call the API. So on Windows
:func:`secure_vault_folder` removes the inherited entries and grants only the current user, then
reads the access list back and refuses to go on if anyone broad can still get in.

The access list is read and written through PowerShell, in the language-independent SDDL form, so
nothing depends on the language of Windows. Any failure to read it counts as "not private".
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

IS_WINDOWS: Final = os.name == "nt"

# Principals that mean "many accounts": Everyone, Builtin Users and Guests, Authenticated Users,
# Anonymous, Interactive and Network logons (SDDL aliases first, then the well-known SIDs).
BROAD_PRINCIPALS: Final = frozenset(
    {
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
        "S-1-5-7",
        "S-1-5-4",
        "S-1-5-2",
    }
)
ACE = re.compile(r"\(([^()]*)\)")
POWERSHELL_TIMEOUT_SECONDS: Final = 60

# The folder arrives in an environment variable, never in the script text, so no path can be
# read as PowerShell code.
READ_SCRIPT: Final = (
    "$ErrorActionPreference='Stop';"
    "(Get-Acl -LiteralPath $env:VAULT_FOLDER).GetSecurityDescriptorSddlForm('Access')"
)
MAKE_PRIVATE_SCRIPT: Final = (
    "$ErrorActionPreference='Stop';"
    "$path=$env:VAULT_FOLDER;"
    "$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;"
    "$acl=Get-Acl -LiteralPath $path;"
    "$acl.SetAccessRuleProtection($true,$false);"
    "foreach($rule in @($acl.Access)){[void]$acl.RemoveAccessRule($rule)};"
    "$grant=New-Object System.Security.AccessControl.FileSystemAccessRule("
    "$user,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
    "$acl.AddAccessRule($grant);"
    "Set-Acl -LiteralPath $path -AclObject $acl"
)


class VaultAccessError(RuntimeError):
    """The vault folder is open to other accounts, or its access list could not be checked."""


DACL_START = re.compile(r"D:(?=(?:P|AI|AR|NO_ACCESS_CONTROL|\())")
SACL_START = re.compile(r"S:(?=(?:P|AI|AR|NO_ACCESS_CONTROL|\())")
# Flags, then entries made only of simple (not nested) parentheses.
DACL_BODY = re.compile(r"(?:P|AI|AR)*((?:\([^()]*\))*)")


def sddl_is_private(sddl: str) -> bool:
    """True if the access list gives no allow entry to a broad group. Fails closed.

    Anything that is not a plain, readable DACL is not private: no ``D:`` part, a "no access
    control" (everyone allowed) list, an entry with fewer than six fields, or text this reader
    does not understand. Deny entries are ignored: they can only take access away.
    """
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
        ace_type, principal = fields[0], fields[5]
        if ace_type in {"A", "OA", "XA", "ZA"} and principal.upper() in BROAD_PRINCIPALS:
            return False
    return True


def _powershell() -> str:  # pragma: no cover - Windows only
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _run(script: str, folder: Path) -> str:  # pragma: no cover - Windows only
    command = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script]
    environment = {**os.environ, "VAULT_FOLDER": str(folder)}
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
        msg = "PowerShell could not read or change the vault folder's permissions"
        raise VaultAccessError(msg)
    return result.stdout


def read_sddl(folder: Path) -> str:  # pragma: no cover - Windows only
    return _run(READ_SCRIPT, folder)


def make_private(folder: Path) -> None:  # pragma: no cover - Windows only
    _run(MAKE_PRIVATE_SCRIPT, folder)


def secure_vault_folder(
    root: Path,
    *,
    created: bool,
    read: Callable[[Path], str] = read_sddl,
    harden: Callable[[Path], None] = make_private,
) -> None:
    """Make ``root`` private to its owner and prove it, or raise :class:`VaultAccessError`.

    A folder this program just created, or an existing one with nothing in it, is hardened. A
    folder that already holds files is only checked: changing who can open someone else's folder
    is not this program's decision, so the error tells the owner what to do.
    """
    if not created and sddl_is_private(read(root)):
        return
    if created or not any(root.iterdir()):
        harden(root)
        if sddl_is_private(read(root)):
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
