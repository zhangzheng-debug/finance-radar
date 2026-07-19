$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "tmp\product_logs\pids.json"
if (-not (Test-Path $PidFile)) {
    Write-Output "No product PID file found."
    exit 0
}
$State = Get-Content -Raw -Encoding UTF8 $PidFile | ConvertFrom-Json
foreach ($Id in @($State.api_pid, $State.web_pid)) {
    $Process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if ($null -ne $Process) {
        Stop-Process -Id $Id
        Write-Output "Stopped PID=$Id"
    }
}
