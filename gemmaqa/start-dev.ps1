#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Start GemmaQA local development stack (backend + frontend).

.DESCRIPTION
  Ports:
    Backend API: http://127.0.0.1:8000
    Frontend UI: http://127.0.0.1:5173

  Default test target: https://thinking-tester-contact-list.herokuapp.com/
  Legal: Only test systems you own or are explicitly authorized to test.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== GemmaQA start-dev ===" -ForegroundColor Cyan
Write-Host "Backend  -> http://127.0.0.1:8000"
Write-Host "Frontend -> http://127.0.0.1:5173"
Write-Host "Default target -> https://thinking-tester-contact-list.herokuapp.com/"
Write-Host ""

$backendEnv = Join-Path $Root "backend\.env"
$backendEnvEx = Join-Path $Root "backend\.env.example"
if (-not (Test-Path $backendEnv) -and (Test-Path $backendEnvEx)) {
  Copy-Item $backendEnvEx $backendEnv
  Write-Host "Created backend\.env from example." -ForegroundColor Yellow
}

$feEnv = Join-Path $Root "frontend\.env"
if (-not (Test-Path $feEnv)) {
  @"
VITE_API_BASE_URL=
VITE_WS_BASE_URL=
VITE_DEMO_MODE=false
"@ | Set-Content -Path $feEnv -Encoding UTF8
  Write-Host "Created frontend\.env (Vite proxy mode)." -ForegroundColor Yellow
}

$venvPython = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  Write-Host "Creating backend virtualenv..." -ForegroundColor Yellow
  python -m venv (Join-Path $Root "backend\.venv")
  & $venvPython -m pip install -r (Join-Path $Root "backend\requirements.txt")
  & $venvPython -m playwright install chromium
}

if (-not (Test-Path (Join-Path $Root "frontend\node_modules"))) {
  Write-Host "Installing frontend deps..." -ForegroundColor Yellow
  Push-Location (Join-Path $Root "frontend"); npm install; Pop-Location
}

$jobs = @()

$npm = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { "npm.cmd" } else { "npm" }

Write-Host "Starting FastAPI backend..." -ForegroundColor Green
$jobs += Start-Process -PassThru -WorkingDirectory (Join-Path $Root "backend") -FilePath $venvPython -ArgumentList @("run.py") -WindowStyle Normal

Write-Host "Starting React frontend..." -ForegroundColor Green
$jobs += Start-Process -PassThru -WorkingDirectory (Join-Path $Root "frontend") -FilePath $npm -ArgumentList @("run", "dev") -WindowStyle Normal

Write-Host ""
Write-Host "All processes launched. PIDs: $($jobs.Id -join ', ')" -ForegroundColor Cyan
Write-Host "Open http://127.0.0.1:5173 — default target is the Thinking Tester Contact List."
Write-Host "Press Ctrl+C in each window to stop, or close the windows."
