[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Stamp,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256,
    [Parameter(Mandatory = $true)][string]$RequiredRelease,
    [string]$SshHost = $env:FINANCE_RADAR_SSH_HOST,
    [string]$IdentityFile = $env:FINANCE_RADAR_SSH_IDENTITY_FILE,
    [string]$BindAddress = "",
    [string]$BackupRoot = "D:\FinanceRadarBackups",
    [string]$PassphraseFile = "D:\FinanceRadarRecovery\finance-radar-backup-passphrase.txt"
)

$ErrorActionPreference = "Stop"
if (-not $SshHost) {
    throw "SshHost is required; pass -SshHost or set FINANCE_RADAR_SSH_HOST"
}
if (-not $IdentityFile) {
    throw "IdentityFile is required; pass -IdentityFile or set FINANCE_RADAR_SSH_IDENTITY_FILE"
}
if ($BindAddress) {
    $parsedBindAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($BindAddress, [ref]$parsedBindAddress) -or
        $parsedBindAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "BindAddress must be a valid IPv4 address"
    }
}
$remoteUser = ($SshHost -split "@", 2)[0]
$remotePrivilege = if ($remoteUser -eq "root") { "" } else { "sudo " }
$releaseIdPattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$'
if ($Stamp -notmatch '^[0-9]{8}T[0-9]{6}Z$' -or $RequiredRelease -notmatch $releaseIdPattern) {
    throw "invalid stamp or release id"
}
if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
    throw "invalid expected SHA-256"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$backupRoot = [System.IO.Path]::GetFullPath($BackupRoot)
$PassphraseFile = [System.IO.Path]::GetFullPath($PassphraseFile)
$backupRootPrefix = $backupRoot.TrimEnd('\') + '\'
if ($PassphraseFile.StartsWith($backupRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "PassphraseFile must stay outside BackupRoot"
}
$auditWorkspaceRoot = [System.IO.Path]::GetFullPath("D:\FinanceRadarScratch\migration-audit")
$destination = [System.IO.Path]::GetFullPath((Join-Path $backupRoot $Stamp))
$rootPrefix = $backupRoot.TrimEnd('\') + '\'
if (-not $destination.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing destination outside backup root"
}

$plain = Join-Path $destination "finance-radar-migration-$Stamp.tgz"
$encrypted = "$plain.aesgcm"
$roundTrip = Join-Path $destination "roundtrip-verify.tgz"
$passphrase = $PassphraseFile
foreach ($required in @($plain, $passphrase, $IdentityFile)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "required file missing: $required"
    }
}

$actualSha256 = (Get-FileHash -LiteralPath $plain -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
    throw "archive SHA-256 mismatch"
}
$listing = & tar -tzf $plain
if ($LASTEXITCODE -ne 0) {
    throw "archive failed tar integrity check"
}
$releaseNeedle = "releases/$RequiredRelease/"
if (-not ($listing | Where-Object { $_ -like "*$releaseNeedle*" } | Select-Object -First 1)) {
    throw "required release missing from archive"
}
$blindNeedle = "releases/$RequiredRelease/artifacts/risk_router_external_blind_v3_report.json"
if (-not ($listing | Where-Object { $_ -like "*$blindNeedle" } | Select-Object -First 1)) {
    throw "external blind v3 report missing from accepted release"
}

& python (Join-Path $PSScriptRoot "backup_crypto.py") encrypt $plain $encrypted --passphrase-file $passphrase
if ($LASTEXITCODE -ne 0) { throw "authenticated encryption failed" }
& python (Join-Path $PSScriptRoot "backup_crypto.py") decrypt $encrypted $roundTrip --passphrase-file $passphrase
if ($LASTEXITCODE -ne 0) { throw "round-trip decryption failed" }
$roundTripSha256 = (Get-FileHash -LiteralPath $roundTrip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($roundTripSha256 -ne $actualSha256) {
    throw "round-trip SHA-256 mismatch"
}
$fullRestoreReport = Join-Path $destination "full-restore-verification.json"
& python (Join-Path $PSScriptRoot "audit_migration_restore.py") `
    $encrypted `
    --passphrase-file $passphrase `
    --expected-release $RequiredRelease `
    --expected-sha256 $actualSha256 `
    --workspace-root $auditWorkspaceRoot `
    --report $fullRestoreReport
if ($LASTEXITCODE -ne 0) {
    throw "full isolated migration restore audit failed"
}
$fullRestoreMarkdown = [System.IO.Path]::ChangeExtension($fullRestoreReport, ".md")
$restoreAudit = Get-Content -Raw -LiteralPath $fullRestoreReport | ConvertFrom-Json
Copy-Item -LiteralPath $fullRestoreReport -Destination (Join-Path $repoRoot "reports\migration_full_restore_latest.json") -Force
Copy-Item -LiteralPath $fullRestoreMarkdown -Destination (Join-Path $repoRoot "reports\migration_full_restore_latest.md") -Force

$verification = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    source = $SshHost
    archive_sha256 = $actualSha256
    round_trip_sha256 = $roundTripSha256
    round_trip_match = $true
    remote_archive = "/var/tmp/finance-radar-migration-$Stamp.tgz"
    local_archive = $encrypted
    encrypted_at_rest = $true
    key_file = $passphrase
    transport = "SSH/SCP retry with keepalive"
    required_release = $RequiredRelease
    full_restore_verified = $true
    full_restore_report = $fullRestoreReport
    full_restore_markdown = $fullRestoreMarkdown
    external_blind_report_included = $true
    model_version = $restoreAudit.release.risk_router_model_version
    model_artifact_sha256 = $restoreAudit.release.risk_router_artifact_sha256
    external_blind_report = $restoreAudit.release.external_blind_report
    external_blind_gate_pass = [bool]$restoreAudit.release.external_blind_gate_pass
    external_blind_promotion = $restoreAudit.release.external_blind_promotion
    ledger_events = [int]$restoreAudit.ledger_restore.counts.canonical_events
    ledger_evidence = [int]$restoreAudit.ledger_restore.counts.event_evidence
    trading_project_included = $false
}
$verification | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination "offhost-verification.json") -Encoding UTF8

# Paths are fixed, absolute children of the verified destination.
Remove-Item -LiteralPath $roundTrip -Force
Remove-Item -LiteralPath $plain -Force

$remoteStage = "/var/tmp/finance-radar-migration-$Stamp"
$remoteArchive = "$remoteStage.tgz"
$sshOptions = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=20")
if ($BindAddress) {
    $sshOptions += @("-o", "BindAddress=$BindAddress")
}
& ssh @sshOptions -i $IdentityFile $SshHost "${remotePrivilege}rm -rf -- '$remoteStage' && ${remotePrivilege}rm -f -- '$remoteArchive'"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "local encrypted copy is verified, but remote temporary cleanup failed"
}

$oldDirectories = Get-ChildItem -LiteralPath $backupRoot -Directory |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 1
foreach ($directory in $oldDirectories) {
    $resolved = [System.IO.Path]::GetFullPath($directory.FullName)
    if (-not $resolved.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to prune outside backup root: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

$verification | ConvertTo-Json
