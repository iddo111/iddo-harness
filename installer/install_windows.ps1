# Iddo Harness — Windows installer (Task Scheduler background service)
#
# Run (not-as-admin, from a regular PowerShell window):
#   iwr -useb https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_windows.ps1 | iex
#
# Or from a local checkout:
#   powershell -ExecutionPolicy Bypass -File .\installer\install_windows.ps1
#
# What this does:
#   1. Checks for Python 3.11+.
#   2. Clones/updates the repo into C:\Program Files\iddo-harness (if elevated)
#      or %LOCALAPPDATA%\iddo-harness (if not).
#   3. Creates a venv there and `pip install -e .`.
#   4. Copies policy.yaml to %LOCALAPPDATA%\iddo-harness\policy.yaml (never clobbers).
#   5. Registers a Task Scheduler task: run at logon, hidden window, restart on
#      failure, action = pythonw -m agent.main --config <path>.
#   6. Triggers the task once and verifies the process actually starts.
#
# PowerShell 5.1+ compatible (default on Windows 10/11). Safe to re-run.

[CmdletBinding()]
param(
    [string]$RepoUrl = "https://github.com/iddo111/iddo-harness.git",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step  { param($n, $msg) Write-Host "`n[$n/8] $msg" -ForegroundColor Yellow }
function Write-Ok    { param($msg) Write-Host "  + $msg" -ForegroundColor Green }
function Write-Warn2 { param($msg) Write-Host "  ! $msg" -ForegroundColor DarkYellow }
function Write-Err2  { param($msg) Write-Host "  x $msg" -ForegroundColor Red }

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Iddo Harness -- Windows service installer" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# [1/8] Am I elevated? Decide install root.
# ---------------------------------------------------------------------------
Write-Step 1 "Checking privileges..."
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($isAdmin) {
    $installDir = "C:\Program Files\iddo-harness"
    Write-Ok "running elevated -> installing to $installDir"
} else {
    $installDir = Join-Path $env:LOCALAPPDATA "iddo-harness"
    Write-Warn2 "not running as Administrator -> installing to $installDir instead"
    Write-Warn2 "(re-run in an elevated PowerShell if you want the Program Files location)"
}
$repoDir   = Join-Path $installDir "src"
$venvDir   = Join-Path $repoDir ".venv"
$configDir = Join-Path $env:LOCALAPPDATA "iddo-harness"
$configPath = Join-Path $configDir "policy.yaml"
$taskName  = "IddoHarness"

# ---------------------------------------------------------------------------
# [2/8] Python 3.11+
# ---------------------------------------------------------------------------
Write-Step 2 "Checking for Python 3.11+..."

function Get-PythonCommand {
    foreach ($cmd in @("python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            try {
                $verOut = & $cmd --version 2>&1
                if ($verOut -match "Python (\d+)\.(\d+)") {
                    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
                    if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                        return @{ Cmd = $cmd; Version = $verOut }
                    }
                }
            } catch { }
        }
    }
    return $null
}

$pyInfo = Get-PythonCommand
if (-not $pyInfo) {
    Write-Err2 "Python 3.11+ not found on PATH."
    Write-Host "  Install it with:  winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host "  Or download from: https://www.python.org/downloads/" -ForegroundColor Yellow
    exit 1
}
Write-Ok "found $($pyInfo.Version) ($($pyInfo.Cmd))"
$pythonCmd = $pyInfo.Cmd

# git
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Err2 "git not found. Install: winget install Git.Git"
    exit 1
}
Write-Ok "git: $((git --version))"

# gh CLI (soft requirement — poller/reporter/cli.py shell out to it)
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Warn2 "GitHub CLI ('gh') not found. Install: winget install GitHub.cli"
    Write-Warn2 "Then run: gh auth login   (needed for the bridge repo poller/reporter)"
} else {
    Write-Ok "gh: $((gh --version | Select-Object -First 1))"
}

# ---------------------------------------------------------------------------
# [3/8] Clone / update repo
# ---------------------------------------------------------------------------
Write-Step 3 "Installing source to $repoDir..."
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

$localSourceRoot = Split-Path -Parent $PSScriptRoot  # .. from installer/
$hasLocalSource = Test-Path (Join-Path $localSourceRoot "pyproject.toml")

if (Test-Path (Join-Path $repoDir ".git")) {
    Write-Host "  updating existing clone..."
    git -C $repoDir fetch --quiet --depth 1 origin
    git -C $repoDir reset --quiet --hard origin/HEAD 2>$null
    Write-Ok "repo updated"
} elseif ($hasLocalSource -and ($localSourceRoot -ne $repoDir)) {
    Write-Host "  installing from local source tree: $localSourceRoot"
    if (Test-Path $repoDir) { Remove-Item -Recurse -Force $repoDir }
    New-Item -ItemType Directory -Force -Path $repoDir | Out-Null
    Copy-Item -Path (Join-Path $localSourceRoot "*") -Destination $repoDir -Recurse -Force `
        -Exclude @(".venv", ".git")
    Write-Ok "copied local source into $repoDir"
} else {
    Write-Host "  cloning $RepoUrl ..."
    if (Test-Path $repoDir) { Remove-Item -Recurse -Force $repoDir }
    git clone --depth 1 $RepoUrl $repoDir
    Write-Ok "cloned to $repoDir"
}

# ---------------------------------------------------------------------------
# [4/8] venv + editable install
# ---------------------------------------------------------------------------
Write-Step 4 "Setting up Python virtual environment..."
if (-not (Test-Path (Join-Path $venvDir "Scripts\python.exe"))) {
    & $pythonCmd -m venv $venvDir
    Write-Ok "created venv at $venvDir"
} else {
    Write-Ok "venv already exists at $venvDir"
}

$venvPython  = Join-Path $venvDir "Scripts\python.exe"
$venvPyw     = Join-Path $venvDir "Scripts\pythonw.exe"
$venvPip     = Join-Path $venvDir "Scripts\pip.exe"
$venvHarness = Join-Path $venvDir "Scripts\iddo-harness.exe"

Write-Host "  installing iddo-harness (pip install -e .)..."
& $venvPython -m pip install --quiet --upgrade pip
if ((Test-Path (Join-Path $repoDir "pyproject.toml")) -or (Test-Path (Join-Path $repoDir "setup.py"))) {
    & $venvPip install --quiet -e $repoDir
} elseif (Test-Path (Join-Path $repoDir "requirements.txt")) {
    Write-Warn2 "no pyproject.toml/setup.py -- installing requirements.txt only (no console script)"
    & $venvPip install --quiet -r (Join-Path $repoDir "requirements.txt")
} else {
    Write-Warn2 "no pyproject.toml/setup.py/requirements.txt found -- falling back to a minimal dependency set"
    & $venvPip install --quiet pyyaml click requests
}
Write-Ok "python dependencies installed into venv"

# ---------------------------------------------------------------------------
# [5/8] Config: %LOCALAPPDATA%\iddo-harness\policy.yaml (never clobber)
# ---------------------------------------------------------------------------
Write-Step 5 "Setting up config at $configPath..."
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
if ((Test-Path $configPath) -and -not $Force) {
    Write-Ok "policy.yaml already exists -- keeping yours"
} else {
    $srcPolicy = Join-Path $repoDir "policy.yaml"
    if (Test-Path $srcPolicy) {
        Copy-Item $srcPolicy $configPath -Force
        Write-Ok "policy.yaml copied to $configPath"
    } else {
        Write-Err2 "no policy.yaml found in $repoDir -- cannot seed default config"
        exit 1
    }
}

# Runtime state dir (audit.log, queue/, results/, last_poll, agent.lock)
$stateDir = Join-Path $env:USERPROFILE ".iddo-harness"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

# ---------------------------------------------------------------------------
# [6/8] Register Task Scheduler task
# ---------------------------------------------------------------------------
Write-Step 6 "Registering Scheduled Task '$taskName'..."

# agent/*.py uses flat, script-style imports (e.g. `from config import
# load_config`), so `agent\` itself must be on PYTHONPATH in addition to the
# repo root -- see tests/conftest.py in the repo for the same requirement.
$agentDir = Join-Path $repoDir "agent"

# Wrap the actual invocation in a small launcher .cmd so we can set
# PYTHONPATH/cwd reliably from a Scheduled Task action (which only takes a
# single Execute + Argument pair).
$launcherPath = Join-Path $installDir "run_harness.cmd"
$launcherContent = @"
@echo off
set PYTHONPATH=$agentDir
set PYTHONUNBUFFERED=1
cd /d "$repoDir"
"$venvPyw" -m agent.main --config "$configPath"
"@
Set-Content -Path $launcherPath -Value $launcherContent -Encoding ASCII
Write-Ok "wrote launcher: $launcherPath"

$action  = New-ScheduledTaskAction -Execute $launcherPath
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -Hidden `
    -MultipleInstances IgnoreNew

$principalArgs = @{ LogonType = "Interactive"; RunLevel = "Limited" }
try {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME @principalArgs
} catch {
    $taskPrincipal = $null
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  task already exists -- updating definition..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

if ($taskPrincipal) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $taskPrincipal -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
}
Write-Ok "registered scheduled task '$taskName' (trigger: at logon, hidden, auto-restart)"

# ---------------------------------------------------------------------------
# [7/8] Trigger once and verify
# ---------------------------------------------------------------------------
Write-Step 7 "Starting task and verifying process comes up..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3

$found = $false
for ($i = 0; $i -lt 5; $i++) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*agent.main*" }
    if ($procs) { $found = $true; break }
    Start-Sleep -Seconds 2
}

if ($found) {
    Write-Ok "agent process is running (PID $($procs[0].ProcessId))"
} else {
    Write-Warn2 "could not confirm a running agent.main process yet."
    Write-Warn2 "Check: Get-ScheduledTaskInfo -TaskName $taskName"
    Write-Warn2 "Check: Get-Content `"$stateDir\audit.log`" -Tail 50"
}

$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "  LastTaskResult: $($taskInfo.LastTaskResult)  LastRunTime: $($taskInfo.LastRunTime)"

# ---------------------------------------------------------------------------
# [8/8] Done
# ---------------------------------------------------------------------------
Write-Step 8 "Done."
Write-Host "`n==============================================" -ForegroundColor Green
Write-Host "  Iddo Harness installed!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Install dir:  $repoDir"
Write-Host "  Config:       $configPath"
Write-Host "  Audit log:    $stateDir\audit.log"
Write-Host ""
Write-Host "  Status:    Get-ScheduledTaskInfo -TaskName $taskName"
Write-Host "  Logs:      Get-Content `"$stateDir\audit.log`" -Tail 50 -Wait"
Write-Host "  Restart:   Stop-ScheduledTask -TaskName $taskName; Start-ScheduledTask -TaskName $taskName"
Write-Host "  Stop:      Stop-ScheduledTask -TaskName $taskName"
Write-Host "  Uninstall: installer\uninstall_windows.ps1"
Write-Host ""
