# Iddo Harness — Windows installer
# הרצה: iwr -useb https://raw.githubusercontent.com/iddo111/iddo-harness/main/installer/install_windows.ps1 | iex

$ErrorActionPreference = "Stop"

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Iddo Harness — Windows installer" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# --- Prerequisites ---
Write-Host "[1/5] Checking prerequisites..." -ForegroundColor Yellow

# Python 3.10+
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "  Python not found. Please install Python 3.10+ from python.org" -ForegroundColor Red
    Write-Host "  Or: winget install Python.Python.3.12" -ForegroundColor Yellow
    exit 1
}
$pyver = python --version 2>&1
Write-Host "  ✓ Python: $pyver"

# git
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Host "  git not found. Install: winget install Git.Git" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ git: $(git --version)"

# gh CLI
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Host "  GitHub CLI not found. Install: winget install GitHub.cli" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ gh: $(gh --version | Select-Object -First 1)"

# --- Clone repo ---
Write-Host "`n[2/5] Cloning iddo-harness..." -ForegroundColor Yellow
$installDir = Join-Path $HOME ".iddo-harness"
$repoDir = Join-Path $installDir "src"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

if (Test-Path $repoDir) {
    Write-Host "  updating existing clone..."
    git -C $repoDir pull --quiet
} else {
    git clone --depth 1 https://github.com/iddo111/iddo-harness.git $repoDir
}

# --- Install Python deps ---
Write-Host "`n[3/5] Installing Python dependencies..." -ForegroundColor Yellow
python -m pip install --user --quiet pyyaml requests

# --- Copy policy.yaml to config dir ---
Write-Host "`n[4/5] Setting up config..." -ForegroundColor Yellow
$configPath = Join-Path $installDir "policy.yaml"
if (-not (Test-Path $configPath)) {
    Copy-Item (Join-Path $repoDir "policy.yaml") $configPath
    Write-Host "  policy.yaml copied to $configPath"
} else {
    Write-Host "  policy.yaml already exists — keeping yours"
}

# --- Create scheduled task ---
Write-Host "`n[5/5] Registering scheduled task (auto-start)..." -ForegroundColor Yellow
$taskName = "IddoHarness"
$script = Join-Path $repoDir "agent\main.py"

$action = New-ScheduledTaskAction -Execute "python" -Argument "`"$script`" --config `"$configPath`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host "`n==============================================" -ForegroundColor Green
Write-Host "  Iddo Harness installed and running!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Config:    $configPath"
Write-Host "  Audit log: $installDir\audit.log"
Write-Host "  Stop:      Stop-ScheduledTask -TaskName IddoHarness"
Write-Host "  Uninstall: Unregister-ScheduledTask -TaskName IddoHarness -Confirm:`$false"
Write-Host ""
