param(
    [switch]$StartDashboard,
    [switch]$NoVSCode
)

$ErrorActionPreference = "Stop"
$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RootDir

function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

Write-Host "Project: $RootDir"

$pythonCmd = $null
if (Test-Command "py") {
    $pythonCmd = "py -3"
} elseif (Test-Command "python") {
    $pythonCmd = "python"
}

if ($null -eq $pythonCmd) {
    Write-Host "Python 3 was not found. Installing Python with winget..."
    if (-not (Test-Command "winget")) {
        throw "winget was not found. Install Python 3 from https://www.python.org/downloads/ and rerun this script."
    }
    winget install --id Python.Python.3.12 -e --source winget
    $pythonCmd = "py -3"
}

Write-Host "Using Python:"
Invoke-Expression "$pythonCmd --version"

if (-not $NoVSCode -and -not (Test-Command "code")) {
    if (Test-Command "winget") {
        Write-Host "VS Code command 'code' was not found. Installing Visual Studio Code..."
        winget install --id Microsoft.VisualStudioCode -e --source winget
    } else {
        Write-Host "VS Code command 'code' was not found. Install VS Code manually if you want an editor."
    }
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment: .venv"
    Invoke-Expression "$pythonCmd -m venv .venv"
}

& ".\.venv\Scripts\Activate.ps1"

python -m pip install --upgrade pip
if ((Test-Path "requirements.txt") -and ((Get-Item "requirements.txt").Length -gt 0)) {
    python -m pip install -r requirements.txt
}

if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example. Fill API keys and SMTP settings before using paid APIs or real sending."
    Copy-Item ".env.example" ".env"
} else {
    Write-Host ".env already exists. Keeping it unchanged."
}

New-Item -ItemType Directory -Force -Path "data" | Out-Null
python -m compileall trade_outreach
python -m trade_outreach.cli init-db

Write-Host ""
Write-Host "Bootstrap complete."
Write-Host "Activate later with: .\.venv\Scripts\Activate.ps1"
Write-Host "Start dashboard with: python -m trade_outreach.dashboard"
Write-Host "Open: http://127.0.0.1:8765"

if ($StartDashboard) {
    python -m trade_outreach.dashboard
}
