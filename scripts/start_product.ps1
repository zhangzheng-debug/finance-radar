param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 8501,
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $Root "tmp\product_logs"
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

if (-not (Test-Path (Join-Path $Root "artifacts\risk_router.joblib"))) {
    & python (Join-Path $Root "scripts\train_risk_router.py")
}

$env:FINANCE_RADAR_API_URL = "http://127.0.0.1:$ApiPort"
$Api = Start-Process python -ArgumentList @("-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "api.log") -RedirectStandardError (Join-Path $LogDir "api.err.log")
$Web = Start-Process python -ArgumentList @("-m", "streamlit", "run", "app/web/Home.py", "--server.headless", "true", "--server.port", "$WebPort", "--browser.gatherUsageStats", "false") -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "web.log") -RedirectStandardError (Join-Path $LogDir "web.err.log")

@{ api_pid = $Api.Id; web_pid = $Web.Id; api_url = "http://127.0.0.1:$ApiPort"; web_url = "http://127.0.0.1:$WebPort" } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $LogDir "pids.json")
Write-Output "API=http://127.0.0.1:$ApiPort"
Write-Output "WEB=http://127.0.0.1:$WebPort"
Write-Output "PIDS=$($Api.Id),$($Web.Id)"
