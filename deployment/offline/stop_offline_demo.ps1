$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root "runtime\pids.json"
if (-not (Test-Path $PidFile)) {
    Write-Output "No offline-demo PID file found."
    exit 0
}
$State = Get-Content -Raw -Encoding UTF8 $PidFile | ConvertFrom-Json
foreach ($ProcessId in @($State.api_pid, $State.web_pid)) {
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -ne $Process) {
        Stop-Process -Id $ProcessId
        Write-Output "Stopped PID=$ProcessId"
    }
}
Remove-Item -LiteralPath $PidFile -Force
