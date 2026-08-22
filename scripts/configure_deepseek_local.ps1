param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $repositoryRoot ".env.local"
}
$targetPath = [IO.Path]::GetFullPath($Destination)
$rootPrefix = $repositoryRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $targetPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "DeepSeek secret target must stay inside the repository workspace."
}

$relativeTarget = [IO.Path]::GetRelativePath($repositoryRoot, $targetPath)
& git -C $repositoryRoot check-ignore -q -- $relativeTarget
if ($LASTEXITCODE -ne 0) {
    throw "Refusing to write a secret to a path that Git does not ignore."
}
if (Test-Path -LiteralPath $targetPath) {
    $existingItem = Get-Item -LiteralPath $targetPath -Force
    if (-not $existingItem.PSIsContainer -and ($existingItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to write the DeepSeek key through a symlink or reparse point."
    }
    if ($existingItem.PSIsContainer) {
        throw "DeepSeek secret target must be a regular file."
    }
}

Write-Host "Paste the DeepSeek API key below. Input is masked and will not be printed."
$secureValue = Read-Host "DEEPSEEK_API_KEY" -AsSecureString
$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
$plainValue = $null
$temporaryPath = $null
try {
    $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    if ([string]::IsNullOrWhiteSpace($plainValue) -or $plainValue -notmatch '^sk-[A-Za-z0-9_-]{20,}$') {
        throw "The supplied DeepSeek API key does not match the expected format."
    }
    $lines = [Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $targetPath) {
        foreach ($line in [IO.File]::ReadAllLines($targetPath)) {
            $lines.Add($line)
        }
    }

    $settings = [ordered]@{
        "DEEPSEEK_API_KEY" = $plainValue
        "FINANCE_RADAR_CAPTURE_LLM_ENABLED" = "1"
        "FINANCE_RADAR_CAPTURE_LLM_PROVIDER" = "deepseek"
        "FINANCE_RADAR_CAPTURE_LLM_MODEL" = "deepseek-v4-flash"
        "FINANCE_RADAR_CAPTURE_LLM_BASE_URL" = "https://api.deepseek.com"
        "FINANCE_RADAR_CAPTURE_LLM_TIMEOUT_SECONDS" = "45"
        "FINANCE_RADAR_CAPTURE_LLM_MAX_TOKENS" = "700"
        # Zero is the explicit unlimited sentinel. Safety remains bounded by
        # batch size, lease, timeout, retry and max-token controls.
        "FINANCE_RADAR_CAPTURE_LLM_DAILY_CNY_CAP" = "0"
        "FINANCE_RADAR_CAPTURE_LLM_DAILY_REQUEST_CAP" = "0"
    }
    foreach ($name in $settings.Keys) {
        $replacement = "$name=$($settings[$name])"
        $matched = $false
        for ($index = 0; $index -lt $lines.Count; $index++) {
            if ($lines[$index] -match ('^' + [Regex]::Escape($name) + '\s*=')) {
                $lines[$index] = $replacement
                $matched = $true
            }
        }
        if (-not $matched) {
            $lines.Add($replacement)
        }
    }

    $temporaryPath = $targetPath + ".tmp-" + [Guid]::NewGuid().ToString("N")
    [IO.File]::WriteAllLines(
        $temporaryPath,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryPath -Destination $targetPath -Force
    $temporaryPath = $null
    Write-Host "DeepSeek configuration saved to ignored local file: $targetPath"
    Write-Host "Model: deepseek-v4-flash | thinking: disabled | daily cap: CNY 1.00"
}
finally {
    if ($temporaryPath -and (Test-Path -LiteralPath $temporaryPath)) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
    $plainValue = $null
}
