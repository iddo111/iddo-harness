# Iddo Harness — Windows uninstaller
#
# Run:
#   powershell -ExecutionPolicy Bypass -File .\installer\uninstall_windows.ps1
#   powershell -ExecutionPolicy Bypass -File .\installer\uninstall_windows.ps1 -Purge -Yes
#
#   (no flags)  Stops & removes the IddoHarness scheduled task and the
#               installed source/venv tree. Leaves policy.yaml and audit
#               log/state dir in place.
#   -Purge      Also deletes %LOCALAPPDATA%\iddo-harness (config + state).
#   -Yes        Skip interactive confirmation prompts.
#
# PowerShell 5.1+ compatible. Safe to re-run.

[CmdletBinding()]
param(
    [switch]$Purge,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$taskName = "IddoHarness"

function Write-Ok    { param($msg) Write-Host "  + $msg" -ForegroundColor Green }
function Write-Warn2 { param($msg) Write-Host "  ! $msg" -ForegroundColor DarkYellow }

function Confirm-Action {
    param([string]$Prompt)
    if ($Yes) { return $true }
    $reply = Read-Host "$Prompt [y/N]"
    return ($reply -match '^[yY]')
}

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Iddo Harness -- Windows uninstaller" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Figure out where it was installed (Program Files if elevated install, else LOCALAPPDATA)
# ---------------------------------------------------------------------------
$candidateDirs = @(
    "C:\Program Files\iddo-harness",
    (Join-Path $env:LOCALAPPDATA "iddo-harness")
)
$configDir = Join-Path $env:LOCALAPPDATA "iddo-harness"
$configPath = Join-Path $configDir "policy.yaml"
$stateDir = Join-Path $env:USERPROFILE ".iddo-harness"

# ---------------------------------------------------------------------------
# [1/3] Stop & unregister scheduled task
# ---------------------------------------------------------------------------
Write-Host "`n[1/3] Removing scheduled task '$taskName'..." -ForegroundColor Yellow
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    try { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue } catch { }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Ok "task '$taskName' stopped and unregistered"
} else {
    Write-Ok "task '$taskName' not found -- nothing to remove"
}

# Also kill any stray running agent process (in case the task already detached)
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*agent.main*" -or $_.CommandLine -like "*iddo-harness*" }
foreach ($p in $procs) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Ok "stopped stray process PID $($p.ProcessId)"
    } catch { }
}

# ---------------------------------------------------------------------------
# [2/3] Remove installed source/venv tree(s)
# ---------------------------------------------------------------------------
Write-Host "`n[2/3] Removing installed source..." -ForegroundColor Yellow
foreach ($dir in $candidateDirs) {
    $srcDir = Join-Path $dir "src"
    $launcher = Join-Path $dir "run_harness.cmd"
    $removedAny = $false
    if (Test-Path $srcDir) {
        Remove-Item -Recurse -Force $srcDir -ErrorAction SilentlyContinue
        Write-Ok "removed $srcDir"
        $removedAny = $true
    }
    if (Test-Path $launcher) {
        Remove-Item -Force $launcher -ErrorAction SilentlyContinue
        $removedAny = $true
    }
    if (-not $removedAny) {
        Write-Ok "$dir not present or already clean -- nothing to remove"
    }
}

# ---------------------------------------------------------------------------
# [3/3] Config + state (only with -Purge)
# ---------------------------------------------------------------------------
Write-Host "`n[3/3] Config and state..." -ForegroundColor Yellow
if ($Purge) {
    if (Test-Path $configDir) {
        if (Confirm-Action "Delete $configDir (your policy.yaml) permanently?") {
            Remove-Item -Recurse -Force $configDir -ErrorAction SilentlyContinue
            Write-Ok "removed $configDir"
        } else {
            Write-Warn2 "kept $configDir"
        }
    } else {
        Write-Ok "$configDir not present"
    }
    if (Test-Path $stateDir) {
        if (Confirm-Action "Delete $stateDir (audit log, queue, results) permanently?") {
            Remove-Item -Recurse -Force $stateDir -ErrorAction SilentlyContinue
            Write-Ok "removed $stateDir"
        } else {
            Write-Warn2 "kept $stateDir"
        }
    } else {
        Write-Ok "$stateDir not present"
    }
} else {
    Write-Ok "kept $configDir and $stateDir (re-run with -Purge to remove those too)"
}

Write-Host "`n==============================================" -ForegroundColor Green
Write-Host "  Iddo Harness uninstalled" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
if (-not $Purge) {
    Write-Host ""
    Write-Host "  Config/policy and state were left in place."
    Write-Host "  Run with -Purge to remove those too:"
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\installer\uninstall_windows.ps1 -Purge"
}
Write-Host ""
