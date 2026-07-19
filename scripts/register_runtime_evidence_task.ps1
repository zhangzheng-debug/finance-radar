[CmdletBinding()]
param(
    [string]$TaskName = "FinanceRadar-Runtime-Evidence",
    [string]$PythonExe = "",
    [string]$KnownLastNonSuccess = "2026-07-18T12:11:35.589624Z"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$captureScript = Join-Path $PSScriptRoot "capture_runtime_evidence.py"
if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction Stop).Source
}
foreach ($required in @($PythonExe, $captureScript)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "required file missing: $required"
    }
}
if ($KnownLastNonSuccess -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$') {
    throw "KnownLastNonSuccess must be an ISO UTC timestamp"
}

$arguments = '"{0}" --known-last-non-success "{1}"' -f $captureScript, $KnownLastNonSuccess
$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 45)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Capture hash-chained Finance Radar 24-hour runtime evidence every 15 minutes." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
