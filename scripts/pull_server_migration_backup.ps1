[CmdletBinding()]
param(
    [string]$SshHost = "root@167.172.69.16",
    [string]$IdentityFile = "C:\Users\MR\.ssh1\id_ed25519",
    [string]$DestinationRoot = "",
    [string]$PassphraseFile = "",
    [int]$LocalRetention = 7,
    [switch]$KeepRemoteTemporary,
    [switch]$KeepPlaintext
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DestinationRoot) {
    $DestinationRoot = Join-Path $repoRoot "server_migration_backup"
}
$DestinationRoot = [System.IO.Path]::GetFullPath($DestinationRoot)
if (-not $PassphraseFile) {
    $PassphraseFile = Join-Path $DestinationRoot ".backup-passphrase"
}
$PassphraseFile = [System.IO.Path]::GetFullPath($PassphraseFile)
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$localRemoteScript = Join-Path $repoRoot "deployment\systemd\create_migration_backup.sh"
$remoteScript = "/tmp/finance-radar-create-migration-$stamp.sh"
$expectedRemoteStage = "/tmp/finance-radar-migration-$stamp"
$expectedRemoteArchive = "$expectedRemoteStage.tgz"
$sshOptions = @(
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    "-o", "ConnectionAttempts=5",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=12"
)

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "SSH identity not found: $IdentityFile"
}
if (-not (Test-Path -LiteralPath $localRemoteScript -PathType Leaf)) {
    throw "migration backup script not found: $localRemoteScript"
}
New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
if (-not $KeepPlaintext -and -not $env:FINANCE_RADAR_BACKUP_PASSPHRASE -and -not (Test-Path -LiteralPath $PassphraseFile -PathType Leaf)) {
    & python (Join-Path $PSScriptRoot "backup_crypto.py") keygen $PassphraseFile
    if ($LASTEXITCODE -ne 0) {
        throw "could not initialize local backup key"
    }
}
& scp @sshOptions -i $IdentityFile $localRemoteScript "${SshHost}:$remoteScript"
if ($LASTEXITCODE -ne 0) {
    throw "could not stage the migration backup script on the server"
}
$remoteOutput = & ssh @sshOptions -i $IdentityFile $SshHost "chmod 700 '$remoteScript' && bash '$remoteScript' '$stamp'"
$remoteExitCode = $LASTEXITCODE
if ($remoteExitCode -ne 0) {
    & ssh @sshOptions -i $IdentityFile $SshHost "rm -rf -- '$expectedRemoteStage' && rm -f -- '$expectedRemoteArchive' '$remoteScript'"
    throw "remote migration backup failed with exit code $remoteExitCode"
}

$archiveLine = $remoteOutput | Where-Object { $_ -match '^archive=' } | Select-Object -Last 1
$stageLine = $remoteOutput | Where-Object { $_ -match '^stage=' } | Select-Object -Last 1
$shaLine = $remoteOutput | Where-Object { $_ -match '^[0-9a-f]{64}\s+' } | Select-Object -First 1
if (-not $archiveLine -or -not $stageLine -or -not $shaLine) {
    throw "remote output did not contain archive, stage and SHA-256"
}
$remoteArchive = ($archiveLine -replace '^archive=', '').Trim()
$remoteStage = ($stageLine -replace '^stage=', '').Trim()
$remoteSha256 = ($shaLine -split '\s+')[0].ToLowerInvariant()
if ($remoteArchive -notmatch '^/tmp/finance-radar-migration-[0-9TZ]+\.tgz$' -or
    $remoteStage -notmatch '^/tmp/finance-radar-migration-[0-9TZ]+$') {
    throw "refusing unexpected remote paths"
}

$destination = Join-Path $DestinationRoot $stamp
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$plainArchive = Join-Path $destination ([System.IO.Path]::GetFileName($remoteArchive))
$scpSucceeded = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    & scp @sshOptions -i $IdentityFile "${SshHost}:$remoteArchive" $plainArchive
    if ($LASTEXITCODE -eq 0) {
        $scpSucceeded = $true
        break
    }
    Write-Warning "SCP attempt $attempt/3 failed with exit code $LASTEXITCODE"
    if ($attempt -lt 3) {
        Start-Sleep -Seconds (2 * $attempt)
    }
}
if (-not $scpSucceeded) {
    throw "SCP failed after 3 attempts; remote temporary archive remains available for safe retry"
}
$localSha256 = (Get-FileHash -LiteralPath $plainArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($localSha256 -ne $remoteSha256) {
    throw "SHA-256 mismatch: remote=$remoteSha256 local=$localSha256"
}
$archiveListing = & tar -tzf $plainArchive
if ($LASTEXITCODE -ne 0) {
    throw "downloaded archive failed tar integrity check"
}
$archiveRoot = [System.IO.Path]::GetFileNameWithoutExtension($remoteArchive)
$currentReleasePath = (& tar -xOf $plainArchive "$archiveRoot/CURRENT_RELEASE.txt").Trim()
if ($LASTEXITCODE -ne 0 -or $currentReleasePath -notmatch '^/opt/finance-radar/releases/([0-9]{8}T[0-9]{6}Z)$') {
    throw "archive CURRENT_RELEASE.txt is missing or invalid"
}
$requiredRelease = [regex]::Match(
    $currentReleasePath,
    '^/opt/finance-radar/releases/([0-9]{8}T[0-9]{6}Z)$'
).Groups[1].Value

$encryptedArchive = $null
$roundTripSha256 = $null
$roundTripMatch = $false
$fullRestoreReport = $null
$fullRestoreVerified = $false
if (-not $KeepPlaintext) {
    $encryptedArchive = "$plainArchive.aesgcm"
    if ($env:FINANCE_RADAR_BACKUP_PASSPHRASE) {
        & python (Join-Path $PSScriptRoot "backup_crypto.py") encrypt $plainArchive $encryptedArchive
    } else {
        & python (Join-Path $PSScriptRoot "backup_crypto.py") encrypt $plainArchive $encryptedArchive --passphrase-file $PassphraseFile
    }
    if ($LASTEXITCODE -ne 0) {
        throw "local authenticated encryption failed"
    }
    $roundTripArchive = Join-Path $destination "roundtrip-verify.tgz"
    if ($env:FINANCE_RADAR_BACKUP_PASSPHRASE) {
        & python (Join-Path $PSScriptRoot "backup_crypto.py") decrypt $encryptedArchive $roundTripArchive
    } else {
        & python (Join-Path $PSScriptRoot "backup_crypto.py") decrypt $encryptedArchive $roundTripArchive --passphrase-file $PassphraseFile
    }
    if ($LASTEXITCODE -ne 0) {
        throw "local encryption round-trip decryption failed"
    }
    $roundTripSha256 = (Get-FileHash -LiteralPath $roundTripArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($roundTripSha256 -ne $localSha256) {
        throw "local encryption round-trip SHA-256 mismatch"
    }
    $roundTripMatch = $true
    $fullRestoreReport = Join-Path $destination "full-restore-verification.json"
    & python (Join-Path $PSScriptRoot "audit_migration_restore.py") `
        $encryptedArchive `
        --passphrase-file $PassphraseFile `
        --expected-release $requiredRelease `
        --expected-sha256 $localSha256 `
        --report $fullRestoreReport
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $roundTripArchive -Force
        Remove-Item -LiteralPath $plainArchive -Force
        throw "full isolated migration restore audit failed"
    }
    $fullRestoreMarkdown = [System.IO.Path]::ChangeExtension($fullRestoreReport, ".md")
    $latestRestoreJson = Join-Path $repoRoot "reports\migration_full_restore_latest.json"
    $latestRestoreMarkdown = Join-Path $repoRoot "reports\migration_full_restore_latest.md"
    Copy-Item -LiteralPath $fullRestoreReport -Destination $latestRestoreJson -Force
    Copy-Item -LiteralPath $fullRestoreMarkdown -Destination $latestRestoreMarkdown -Force
    $fullRestoreVerified = $true
    Remove-Item -LiteralPath $roundTripArchive -Force
    Remove-Item -LiteralPath $plainArchive -Force
}

$verification = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    source = $SshHost
    archive_sha256 = $localSha256
    round_trip_sha256 = $roundTripSha256
    round_trip_match = $roundTripMatch
    required_release = $requiredRelease
    full_restore_verified = $fullRestoreVerified
    full_restore_report = $fullRestoreReport
    full_restore_markdown = if ($fullRestoreReport) { [System.IO.Path]::ChangeExtension($fullRestoreReport, ".md") } else { $null }
    archive_entries = @($archiveListing).Count
    remote_archive = $remoteArchive
    local_archive = if ($encryptedArchive) { $encryptedArchive } else { $plainArchive }
    encrypted_at_rest = [bool]$encryptedArchive
    key_file = if ($encryptedArchive -and -not $env:FINANCE_RADAR_BACKUP_PASSPHRASE) { $PassphraseFile } else { "environment" }
    transport = "SSH/SCP"
    trading_project_included = $false
}
$verification | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination "offhost-verification.json") -Encoding UTF8

if (-not $KeepRemoteTemporary) {
    & ssh @sshOptions -i $IdentityFile $SshHost "rm -rf -- '$remoteStage' && rm -f -- '$remoteArchive' '$remoteScript'"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "local copy is verified, but remote temporary cleanup failed"
    }
} else {
    & ssh @sshOptions -i $IdentityFile $SshHost "rm -f -- '$remoteScript'"
}

$resolvedRoot = [System.IO.Path]::GetFullPath($DestinationRoot).TrimEnd('\') + '\'
$oldDirectories = Get-ChildItem -LiteralPath $DestinationRoot -Directory |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip ([Math]::Max(1, $LocalRetention))
foreach ($directory in $oldDirectories) {
    $resolved = [System.IO.Path]::GetFullPath($directory.FullName)
    if (-not $resolved.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to prune outside destination root: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

$verification | ConvertTo-Json
