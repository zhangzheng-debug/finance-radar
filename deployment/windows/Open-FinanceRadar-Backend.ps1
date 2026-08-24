[CmdletBinding()]
param(
    [string]$ConfigPath = "D:\FinanceRadar\owner-backend.json",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptRoot "..\..")
$launcher = Join-Path $repoRoot "scripts\open_internal_ui.py"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    $configDirectory = Split-Path -Parent $ConfigPath
    if ([IO.Path]::GetPathRoot($configDirectory) -ne "D:\") {
        throw "首次配置必须保存在 D:，避免占用 C:。"
    }
    New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
    Write-Host "首次使用：这里只保存 SSH 地址和 D: 私钥路径，不保存令牌。"
    $sshHost = Read-Host "SSH 地址（例如 ubuntu@server.example）"
    $identityFile = Read-Host "D: 上的 SSH 私钥路径；使用 ssh-agent 时可留空"
    [ordered]@{
        schema_version = 1
        host = $sshHost
        identity_file = $identityFile
    } | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
}

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ($config.schema_version -ne 1 -or [string]::IsNullOrWhiteSpace($config.host)) {
    throw "老板入口配置无效：$ConfigPath"
}
if (-not [string]::IsNullOrWhiteSpace($config.identity_file)) {
    $resolvedIdentity = Resolve-Path -LiteralPath $config.identity_file
    if ([IO.Path]::GetPathRoot($resolvedIdentity.Path) -ne "D:\") {
        throw "SSH 私钥必须位于 D:。"
    }
}

$arguments = @($launcher, "--host", [string]$config.host, "--role", "admin")
if (-not [string]::IsNullOrWhiteSpace($config.identity_file)) {
    $arguments += @("--identity-file", [string]$resolvedIdentity.Path)
}
if ($DryRun) {
    $arguments += "--dry-run"
}

Write-Host "正在打开 Finance Radar 老板总览。保持本窗口开启；结束时按 Ctrl+C。"
& python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "老板入口启动失败（退出码 $LASTEXITCODE）。"
}
