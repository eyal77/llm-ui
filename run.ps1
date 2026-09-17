# Windows: creates .venv on first run, installs requirements, starts the app.
#   powershell -ExecutionPolicy Bypass -File .\run.ps1            # http://127.0.0.1:8000
#   .\run.ps1 -Port 8080
param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}
$py = Join-Path ".venv" "Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt

if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env from .env.example" }

Write-Host "LLM Compare: http://127.0.0.1:$Port"
& $py -m uvicorn app.main:app --host 127.0.0.1 --port $Port --reload
