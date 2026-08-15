[CmdletBinding()]
param(
    [string]$TaskName = "FinanceRadar-Offhost-Backup",
    [string]$At = "02:30",
    [string]$BackupScript = "",
    [string]$SshHost = $env:FINANCE_RADAR_SSH_HOST,
    [string]$IdentityFile = $env:FINANCE_RADAR_SSH_IDENTITY_FILE
)

$ErrorActionPreference = "Stop"
if (-not $SshHost) {
    throw "SshHost is required; pass -SshHost or set FINANCE_RADAR_SSH_HOST"
}
if (-not $IdentityFile) {
    throw "IdentityFile is required; pass -IdentityFile or set FINANCE_RADAR_SSH_IDENTITY_FILE"
}
if (-not $BackupScript) {
    $BackupScript = Join-Path $PSScriptRoot "pull_server_migration_backup.ps1"
}
$BackupScript = [System.IO.Path]::GetFullPath($BackupScript)
if (-not (Test-Path -LiteralPath $BackupScript -PathType Leaf)) {
    throw "backup script not found: $BackupScript"
}
$time = [datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`" -SshHost `"$SshHost`" -IdentityFile `"$IdentityFile`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Create, verify, SSH-transfer and AES-GCM encrypt a Finance Radar AWS migration backup." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author, Description
