[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Stamp,
    [Parameter(Mandatory = $true)][string]$ExpectedSha256,
    [Parameter(Mandatory = $true)][string]$RequiredRelease,
    [string]$SshHost = "ubuntu@18.208.34.152",
    [string]$IdentityFile = "C:\Users\MR\.ssh1\id_ed25519",
    [switch]$SkipRemoteCleanup
)

$ErrorActionPreference = "Stop"
if ($Stamp -notmatch '^[0-9]{8}T[0-9]{6}Z$' -or $RequiredRelease -notmatch '^[0-9]{8}T[0-9]{6}Z$') {
    throw "invalid stamp or release id"
}
if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
    throw "invalid expected SHA-256"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$backupRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "server_migration_backup"))
$destination = [System.IO.Path]::GetFullPath((Join-Path $backupRoot $Stamp))
$rootPrefix = $backupRoot.TrimEnd('\') + '\'
if (-not $destination.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing destination outside backup root"
}

$encrypted = Join-Path $destination "finance-radar-migration-$Stamp.tgz.aesgcm"
$roundTrip = Join-Path $destination "roundtrip-verify.tgz"
$passphrase = Join-Path $backupRoot ".backup-passphrase"
foreach ($required in @($encrypted, $passphrase, $IdentityFile)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "required file missing: $required"
    }
}

try {
    & python (Join-Path $PSScriptRoot "backup_crypto.py") decrypt $encrypted $roundTrip --passphrase-file $passphrase
    if ($LASTEXITCODE -ne 0) { throw "round-trip decryption failed" }
    $roundTripSha256 = (Get-FileHash -LiteralPath $roundTrip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($roundTripSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "round-trip SHA-256 mismatch"
    }

    $listing = & tar -tzf $roundTrip
    if ($LASTEXITCODE -ne 0) { throw "archive failed tar integrity check" }
    $releaseNeedle = "releases/$RequiredRelease/"
    if (-not ($listing | Where-Object { $_ -like "*$releaseNeedle*" } | Select-Object -First 1)) {
        throw "required release missing from archive"
    }
    $blindNeedle = "releases/$RequiredRelease/artifacts/risk_router_external_blind_v3_report.json"
    if (-not ($listing | Where-Object { $_ -like "*$blindNeedle" } | Select-Object -First 1)) {
        throw "external blind v3 report missing from accepted release"
    }
    if (-not ($listing | Where-Object { $_ -like "*releases/$RequiredRelease/scripts/official_event_collector.py" } | Select-Object -First 1)) {
        throw "current official source collector missing from archive"
    }
    if ($listing | Where-Object { $_ -match '(^|/)ethusdc-pivot-bot(/|$)' } | Select-Object -First 1) {
        throw "trading project unexpectedly present in archive"
    }

    $verification = [ordered]@{
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        source = $SshHost
        archive_sha256 = $roundTripSha256
        round_trip_sha256 = $roundTripSha256
        round_trip_match = $true
        archive_entries = @($listing).Count
        remote_archive = "/tmp/finance-radar-migration-$Stamp.tgz"
        local_archive = $encrypted
        encrypted_at_rest = $true
        key_file = $passphrase
        transport = "SSH/SCP with keepalive and retry"
        required_release = $RequiredRelease
        required_release_included = $true
        external_blind_report_included = $true
        official_source_collector_included = $true
        trading_project_included = $false
    }
    $verification | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination "offhost-verification.json") -Encoding UTF8
} finally {
    if (Test-Path -LiteralPath $roundTrip -PathType Leaf) {
        Remove-Item -LiteralPath $roundTrip -Force
    }
}

if (-not $SkipRemoteCleanup) {
    $remoteStage = "/tmp/finance-radar-migration-$Stamp"
    $remoteArchive = "$remoteStage.tgz"
    $sshOptions = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=6")
    $cleaned = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        & ssh @sshOptions -i $IdentityFile $SshHost "rm -rf -- '$remoteStage' && rm -f -- '$remoteArchive'"
        if ($LASTEXITCODE -eq 0) {
            $cleaned = $true
            break
        }
        if ($attempt -lt 3) { Start-Sleep -Seconds (2 * $attempt) }
    }
    if (-not $cleaned) {
        Write-Warning "encrypted local copy is verified, but remote temporary cleanup still failed"
    }
}

$verification | ConvertTo-Json
