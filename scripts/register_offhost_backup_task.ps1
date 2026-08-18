[CmdletBinding()]
param(
    [string]$TaskName = "FinanceRadar-Offhost-Backup",
    [string]$At = "02:30",
    [string]$BackupScript = "",
    [string]$SshHost = $env:FINANCE_RADAR_SSH_HOST,
    [string]$IdentityFile = $env:FINANCE_RADAR_SSH_IDENTITY_FILE,
    [string]$DestinationRoot = "D:\FinanceRadarBackups",
    [string]$PassphraseFile = "D:\FinanceRadarRecovery\finance-radar-backup-passphrase.txt"
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
$IdentityFile = [System.IO.Path]::GetFullPath($IdentityFile)
$DestinationRoot = [System.IO.Path]::GetFullPath($DestinationRoot)
$PassphraseFile = [System.IO.Path]::GetFullPath($PassphraseFile)
if (-not (Test-Path -LiteralPath $BackupScript -PathType Leaf)) {
    throw "backup script not found: $BackupScript"
}
$destinationPrefix = $DestinationRoot.TrimEnd('\') + '\'
if ($PassphraseFile.StartsWith($destinationPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "PassphraseFile must stay outside DestinationRoot"
}
$time = [datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$argument = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$BackupScript`" -SshHost `"$SshHost`" -IdentityFile `"$IdentityFile`" -DestinationRoot `"$DestinationRoot`" -PassphraseFile `"$PassphraseFile`" -LocalRetention 1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -Hidden `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Create, verify, SSH-transfer and AES-GCM encrypt a Finance Radar AWS migration backup." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Author, Description
