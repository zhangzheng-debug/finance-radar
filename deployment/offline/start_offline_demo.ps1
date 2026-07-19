param(
    [int]$ApiPort = 18700,
    [int]$WebPort = 18701,
    [string]$Python = "python",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root "runtime"
$PidFile = Join-Path $Runtime "pids.json"
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null

if (Test-Path $PidFile) {
    throw "Offline demo already has a PID file. Run stop_offline_demo.ps1 first."
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root$([IO.Path]::PathSeparator)$env:PYTHONPATH" } else { $Root }
$env:FINANCE_RADAR_DB = Join-Path $Root "data\finance_radar_demo.sqlite3"
$env:FINANCE_RADAR_OPS_DB = Join-Path $Root "data\finance_radar_demo_operations.sqlite3"
$env:FINANCE_RADAR_ARTIFACT_DIR = Join-Path $Root "artifacts"
$env:FINANCE_RADAR_EVIDENCE_OBJECT_DIR = Join-Path $Root "data\evidence_objects"
$env:FINANCE_RADAR_REPLAY_DIR = Join-Path $Root "replay\cases"
$env:FINANCE_RADAR_API_URL = "http://127.0.0.1:$ApiPort"
$env:FINANCE_RADAR_WEB_URL = "http://127.0.0.1:$WebPort"
$env:FINANCE_RADAR_DEMO_MODE = "REPLAY"
$env:FINANCE_RADAR_ADMIN_TOKEN = "offline-demo-local-only"
$env:FINANCE_RADAR_OFFLINE_NETWORK_GUARD = "1"
$env:FINANCE_RADAR_SHOW_DEBUG = "0"
$env:FINANCE_RADAR_REVIEW_UI_ENABLED = "0"

foreach ($Name in @(
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
    "BINANCE_API_KEY", "BINANCE_API_SECRET", "IBKR_ACCOUNT",
    "FINANCE_RADAR_EVIDENCE_LLM_URL"
)) {
    Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
}

& $Python -c "import fastapi, uvicorn, streamlit, sklearn, joblib, pandas, httpx"
if ($LASTEXITCODE -ne 0) {
    throw "Offline runtime dependencies are missing. See README_OFFLINE.md."
}

$Api = Start-Process $Python -ArgumentList @(
    "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", "$ApiPort"
) -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Runtime "api.log") `
  -RedirectStandardError (Join-Path $Runtime "api.err.log")

$Web = Start-Process $Python -ArgumentList @(
    "-m", "streamlit", "run", "app/web/Home.py", "--server.headless", "true",
    "--server.address", "127.0.0.1", "--server.port", "$WebPort",
    "--browser.gatherUsageStats", "false"
) -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Runtime "web.log") `
  -RedirectStandardError (Join-Path $Runtime "web.err.log")

$State = @{
    api_pid = $Api.Id
    web_pid = $Web.Id
    api_url = "http://127.0.0.1:$ApiPort"
    web_url = "http://127.0.0.1:$WebPort"
    offline_network_guard = $true
    trading_capability = $false
}
$State | ConvertTo-Json | Set-Content -Encoding UTF8 $PidFile

try {
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/v1/health" -TimeoutSec 1
            if ($Health.data.status -eq "ok") { $Ready = $true; break }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $Ready) { throw "Offline API did not become healthy." }
    Write-Output "OFFLINE_GUARD=ENABLED"
    Write-Output "TRADING_CAPABILITY=ABSENT"
    Write-Output "API=http://127.0.0.1:$ApiPort"
    Write-Output "WEB=http://127.0.0.1:$WebPort"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$WebPort" }
} catch {
    & (Join-Path $Root "stop_offline_demo.ps1")
    throw
}
