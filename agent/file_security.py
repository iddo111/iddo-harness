"""Cross-platform protection and verification for private harness files."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


_WINDOWS_LOCK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:IDDO_PRIVATE_FILE
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.AddAccessRule($rule)
[System.IO.File]::SetAccessControl($path, $acl)
"""

_WINDOWS_VERIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:IDDO_PRIVATE_FILE
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = [System.Security.AccessControl.FileSecurity]::new(
    $path,
    [System.Security.AccessControl.AccessControlSections]::Access
)
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
$allowed = @($rules | Where-Object { $_.AccessControlType -eq 'Allow' })
$bad = @($rules | Where-Object { $_.IsInherited -or $_.IdentityReference.Value -ne $sid })
if ($allowed.Count -lt 1 -or $bad.Count -gt 0) { exit 3 }
exit 0
"""


def _run_windows_acl_script(path: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["IDDO_PRIVATE_FILE"] = str(path.resolve())
    return subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def restrict_private_file(path: Path | str) -> None:
    """Restrict *path* to the current OS identity, failing closed on error."""
    target = Path(path)
    if os.name != "nt":
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    else:
        result = _run_windows_acl_script(target, _WINDOWS_LOCK_SCRIPT)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PermissionError(f"could not protect private file {target}: {detail}")

    if not private_file_permissions_ok(target):
        raise PermissionError(f"private-file permission verification failed: {target}")


def private_file_permissions_ok(path: Path | str) -> bool:
    """Return whether *path* is accessible only to the current identity."""
    target = Path(path)
    if not target.is_file():
        return False
    if os.name != "nt":
        return stat.S_IMODE(target.stat().st_mode) == 0o600
    return _run_windows_acl_script(target, _WINDOWS_VERIFY_SCRIPT).returncode == 0
