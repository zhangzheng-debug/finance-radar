[CmdletBinding()]
param(
    [string]$SshHost = "",
    [string]$IdentityFile = "C:\Users\MR\.ssh1\id_ed25519",
    [Parameter(Mandatory = $true)]
    [string]$EncryptedArchive,
    [string]$PassphraseFile = "",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]{8}T[0-9]{6}Z$')]
    [string]$ExpectedRelease,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ExpectedSha256,
    [string]$PublicWebUrl = "",
    [switch]$Activate,
    [switch]$AllowCurrentServer
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$EncryptedArchive = [System.IO.Path]::GetFullPath($EncryptedArchive)
if (-not $PassphraseFile) {
    $PassphraseFile = Join-Path $repoRoot "server_migration_backup\.backup-passphrase"
}
$PassphraseFile = [System.IO.Path]::GetFullPath($PassphraseFile)
$IdentityFile = [System.IO.Path]::GetFullPath($IdentityFile)

if (-not (Test-Path -LiteralPath $EncryptedArchive -PathType Leaf)) {
    throw "encrypted migration archive not found: $EncryptedArchive"
}
if (-not (Test-Path -LiteralPath $PassphraseFile -PathType Leaf)) {
    throw "backup passphrase file not found: $PassphraseFile"
}
if ($Activate) {
    if (-not $SshHost -or $SshHost -notmatch '^(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+$') {
        throw "a simple user@host SSH target is required for activation"
    }
    if ($SshHost -match '(^|@)167\.172\.69\.16$' -and -not $AllowCurrentServer) {
        throw "refusing the current VPS; activation is only for a clean replacement host"
    }
    if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
        throw "SSH identity not found: $IdentityFile"
    }
    try {
        $publicUri = [Uri]$PublicWebUrl
    } catch {
        throw "PublicWebUrl is invalid"
    }
    if ($publicUri.Scheme -ne 'https' -or
        $publicUri.AbsolutePath.TrimEnd('/') -notmatch '/radar$' -or
        $publicUri.Query -or $publicUri.Fragment) {
        throw "PublicWebUrl must be an HTTPS URL ending in /radar without query or fragment"
    }
    if ($PublicWebUrl -notmatch '^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/radar/?$') {
        throw "PublicWebUrl must use a simple DNS name or IP, optional port, and the /radar path"
    }
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$tempRoot = [System.IO.Path]::GetFullPath($env:TEMP)
$localWork = Join-Path $tempRoot "finance-radar-cutover-$stamp-$([guid]::NewGuid().ToString('N'))"
$plainArchiveName = [System.IO.Path]::GetFileNameWithoutExtension(
    [System.IO.Path]::GetFileName($EncryptedArchive)
)
if ($plainArchiveName -notmatch '^finance-radar-migration-[0-9]{8}T[0-9]{6}Z\.tgz$') {
    throw "encrypted archive filename is invalid"
}
$plainArchive = Join-Path $localWork $plainArchiveName
$localPrepared = Join-Path $localWork "finance-radar-restore-$stamp.prepared"
$auditReport = Join-Path $localWork "encrypted-audit.json"
$prepareReport = Join-Path $localWork "service-restore-preparation.json"
$latestAuditReport = Join-Path $repoRoot "reports\new_vps_encrypted_restore_audit_latest.json"
$latestPrepareReport = Join-Path $repoRoot "reports\migration_service_restore_drill_latest.json"
$latestPreflightReport = Join-Path $repoRoot "reports\replacement_vps_preflight_latest.json"
$remoteArchive = "/tmp/$plainArchiveName"
$remotePrepared = "/tmp/finance-radar-restore-$stamp.prepared"
$remotePrepareScript = "/tmp/prepare_migration_restore-$stamp.py"
$remoteAuditScript = "/tmp/audit_migration_restore.py"
$remoteCryptoScript = "/tmp/backup_crypto.py"
$remoteActivateScript = "/tmp/activate_prepared_restore-$stamp.sh"
$remotePreflightScript = "/tmp/replacement_vps_preflight-$stamp.py"
$remotePreflightReport = "/tmp/replacement-vps-preflight-$stamp.json"
$sshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=12')

New-Item -ItemType Directory -Path $localWork | Out-Null
try {
    & python (Join-Path $PSScriptRoot 'audit_migration_restore.py') `
        $EncryptedArchive `
        --passphrase-file $PassphraseFile `
        --expected-release $ExpectedRelease `
        --expected-sha256 $ExpectedSha256.ToLowerInvariant() `
        --report $auditReport
    if ($LASTEXITCODE -ne 0) { throw "encrypted migration audit failed" }

    & python (Join-Path $PSScriptRoot 'backup_crypto.py') decrypt `
        $EncryptedArchive $plainArchive --passphrase-file $PassphraseFile
    if ($LASTEXITCODE -ne 0) { throw "authenticated decryption failed" }

    & python (Join-Path $PSScriptRoot 'prepare_migration_restore.py') `
        $plainArchive $localPrepared `
        --expected-release $ExpectedRelease `
        --expected-sha256 $ExpectedSha256.ToLowerInvariant() `
        --report $prepareReport
    if ($LASTEXITCODE -ne 0) { throw "full service-restore preparation failed" }
    Copy-Item -LiteralPath $auditReport -Destination $latestAuditReport -Force
    Copy-Item -LiteralPath ([System.IO.Path]::ChangeExtension($auditReport, '.md')) `
        -Destination ([System.IO.Path]::ChangeExtension($latestAuditReport, '.md')) -Force
    Copy-Item -LiteralPath $prepareReport -Destination $latestPrepareReport -Force
    Copy-Item -LiteralPath ([System.IO.Path]::ChangeExtension($prepareReport, '.md')) `
        -Destination ([System.IO.Path]::ChangeExtension($latestPrepareReport, '.md')) -Force

    if (-not $Activate) {
        [pscustomobject]@{
            status = 'AUDIT_ONLY_PASS'
            encrypted_archive = $EncryptedArchive
            expected_release = $ExpectedRelease
            encrypted_audit = $latestAuditReport
            service_restore_preparation = $latestPrepareReport
            activation_performed = $false
        } | ConvertTo-Json
        return
    }

    & ssh @sshOptions -i $IdentityFile $SshHost `
        "test ! -e /opt/finance-radar && ! compgen -G '/etc/systemd/system/finance-radar-*.service' >/dev/null"
    if ($LASTEXITCODE -ne 0) {
        throw "replacement VPS is not clean; refusing activation"
    }

    $prepareState = Get-Content -LiteralPath $prepareReport -Raw | ConvertFrom-Json
    $expectedUnpackedBytes = [Int64]$prepareState.unpacked_bytes_scanned
    if ($expectedUnpackedBytes -le 0) {
        throw "service-restore report did not provide a positive unpacked byte count"
    }
    & scp -O @sshOptions -i $IdentityFile `
        (Join-Path $PSScriptRoot 'replacement_vps_preflight.py') "${SshHost}:$remotePreflightScript"
    if ($LASTEXITCODE -ne 0) { throw "replacement VPS preflight transfer failed" }
    $preflightOutput = & ssh @sshOptions -i $IdentityFile $SshHost `
        "python3 '$remotePreflightScript' --expected-unpacked-bytes '$expectedUnpackedBytes' --public-web-url '$PublicWebUrl' --require-edge-tools --report '$remotePreflightReport'"
    $preflightExitCode = $LASTEXITCODE
    $preflightOutput | Write-Output
    & scp -O @sshOptions -i $IdentityFile `
        "${SshHost}:$remotePreflightReport" $latestPreflightReport
    $preflightCopyExitCode = $LASTEXITCODE
    & ssh @sshOptions -i $IdentityFile $SshHost `
        "rm -f '$remotePreflightScript' '$remotePreflightReport'" | Out-Null
    if ($preflightCopyExitCode -ne 0) { throw "replacement VPS preflight report retrieval failed" }
    if ($preflightExitCode -ne 0) { throw "replacement VPS preflight failed; see $latestPreflightReport" }

    & scp -O @sshOptions -i $IdentityFile `
        $plainArchive "${SshHost}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw "plaintext archive transfer failed" }
    & scp -O @sshOptions -i $IdentityFile `
        (Join-Path $PSScriptRoot 'prepare_migration_restore.py') "${SshHost}:$remotePrepareScript"
    if ($LASTEXITCODE -ne 0) { throw "restore preparer transfer failed" }
    & scp -O @sshOptions -i $IdentityFile `
        (Join-Path $PSScriptRoot 'audit_migration_restore.py') "${SshHost}:$remoteAuditScript"
    if ($LASTEXITCODE -ne 0) { throw "restore audit dependency transfer failed" }
    & scp -O @sshOptions -i $IdentityFile `
        (Join-Path $PSScriptRoot 'backup_crypto.py') "${SshHost}:$remoteCryptoScript"
    if ($LASTEXITCODE -ne 0) { throw "restore crypto dependency transfer failed" }
    & scp -O @sshOptions -i $IdentityFile `
        (Join-Path $repoRoot 'deployment\systemd\activate_prepared_restore.sh') "${SshHost}:$remoteActivateScript"
    if ($LASTEXITCODE -ne 0) { throw "activation script transfer failed" }

    $remoteCommand = @"
set -euo pipefail
cleanup() {
  rm -f '$remoteArchive' '$remotePrepareScript' '$remoteAuditScript' '$remoteCryptoScript' '$remoteActivateScript'
  if [ -d '$remotePrepared' ]; then rm -rf '$remotePrepared'; fi
}
trap cleanup EXIT
python3 '$remotePrepareScript' '$remoteArchive' '$remotePrepared' --expected-release '$ExpectedRelease' --expected-sha256 '$($ExpectedSha256.ToLowerInvariant())'
bash '$remoteActivateScript' '$remotePrepared' '$ExpectedRelease' '$PublicWebUrl' --activate
"@
    & ssh @sshOptions -i $IdentityFile $SshHost $remoteCommand
    if ($LASTEXITCODE -ne 0) { throw "replacement VPS activation failed" }

    [pscustomobject]@{
        status = 'ACTIVATION_PASS'
        target = $SshHost
        release = $ExpectedRelease
        public_web_url = $PublicWebUrl
        preflight_report = $latestPreflightReport
        nginx_tls = 'PENDING_SEPARATE_CUTOVER'
    } | ConvertTo-Json
} finally {
    $resolvedWork = [System.IO.Path]::GetFullPath($localWork)
    if (-not $resolvedWork.StartsWith($tempRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing unsafe temporary cleanup target"
    }
    if (Test-Path -LiteralPath $resolvedWork) {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}
