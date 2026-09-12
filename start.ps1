# ZCode Universal Model Gateway - Windows PowerShell launcher.
#
# Finds a Python 3.11+ interpreter, installs dependencies if needed, then
# starts the gateway. No machine-specific paths are hard-coded: if you have
# activated a conda env or a venv, that "python" is used automatically.
#
# Never put API keys in this file - set them as environment variables instead.
# User-facing text stays ASCII here; the gateway prints the Chinese banner.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:GATEWAY_HOST) { $env:GATEWAY_HOST = "127.0.0.1" }
if (-not $env:GATEWAY_PORT) { $env:GATEWAY_PORT = "8787" }

function Test-Python {
    param([string]$Exe)
    try {
        & $Exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# --- find a suitable Python -------------------------------------------------
$candidates = @()
foreach ($name in @("python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
}
$candidates += @(
    (Join-Path $env:USERPROFILE "miniconda3\python.exe"),
    (Join-Path $env:USERPROFILE "anaconda3\python.exe"),
    (Join-Path $env:LOCALAPPDATA "miniconda3\python.exe"),
    (Join-Path $env:LOCALAPPDATA "anaconda3\python.exe"),
    (Join-Path $env:ProgramData "miniconda3\python.exe"),
    (Join-Path $env:ProgramData "Anaconda3\python.exe")
)
if ($env:CONDA_PREFIX) {
    $candidates += (Join-Path $env:CONDA_PREFIX "python.exe")
}

$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate -ErrorAction SilentlyContinue) -and (Test-Python $candidate)) {
        $python = $candidate
        break
    }
    # "python"/"python3" resolved via PATH are command names, not paths.
    if ($candidate -and -not (Test-Path $candidate -ErrorAction SilentlyContinue) -and (Test-Python $candidate)) {
        $python = $candidate
        break
    }
}

if (-not $python) {
    Write-Host "[ERROR] No Python 3.11+ found." -ForegroundColor Red
    Write-Host "        Install Python from https://www.python.org/ or Miniconda,"
    Write-Host "        make sure 'python' is on your PATH, then run this again."
    exit 1
}
Write-Host "Using Python: $python"

# --- dependencies -----------------------------------------------------------
& $python -c 'import fastapi, uvicorn, httpx, yaml, pydantic' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing dependencies (first run only)..." -ForegroundColor Cyan
    & $python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to install dependencies. Check your network/proxy and retry." -ForegroundColor Red
        exit 1
    }
}

# --- run --------------------------------------------------------------------
& $python -m gateway
