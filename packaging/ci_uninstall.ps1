# Silent uninstall. User data under %LOCALAPPDATA%\StrategyBuilder stays,
# unless -DeleteUserData is passed (the /DELETEUSERDATA switch).

param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [switch]$DeleteUserData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ci_process.ps1")

$unins = Join-Path $Destination "unins000.exe"
if (-not (Test-Path -LiteralPath $unins)) {
    throw "uninstaller not found: $unins"
}

$userRoot = Join-Path $env:LOCALAPPDATA "StrategyBuilder"
$runs = Join-Path $userRoot "runs"
$marker = Join-Path $runs "ci-preserve.txt"
if (-not $DeleteUserData) {
    New-Item -ItemType Directory -Force -Path $runs | Out-Null
    Set-Content -LiteralPath $marker -Value "keep" -Encoding ascii
}

$log = Join-Path $env:TEMP "strategy-builder-uninstall.log"
if (Test-Path -LiteralPath $log) {
    Remove-Item -LiteralPath $log -Force
}

$flag = ""
if ($DeleteUserData) {
    $flag = " /DELETEUSERDATA"
}
$arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART$flag /LOG=""$log"""
$code = Wait-GuiProcess -File $unins -Arguments $arguments
Write-Output "uninstaller exit $code"

$deadline = (Get-Date).AddSeconds(90)
$cli = Join-Path $Destination "backtester.exe"
while ((Test-Path -LiteralPath $cli) -and ((Get-Date) -lt $deadline)) {
    Start-Sleep -Seconds 1
}
if (Test-Path -LiteralPath $cli) {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
    throw "uninstall left $cli (exit $code)"
}
if ($code -ne 0) {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
    throw "uninstaller exited $code"
}
if ($DeleteUserData) {
    if (Test-Path -LiteralPath $userRoot) {
        throw "DELETEUSERDATA left $userRoot"
    }
    Write-Output "uninstalled $Destination"
    Write-Output "deleted $userRoot"
    return
}
if (-not (Test-Path -LiteralPath $marker)) {
    throw "uninstall deleted $marker"
}
$config = Join-Path $userRoot "config.json"
if (-not (Test-Path -LiteralPath $config)) {
    throw "uninstall deleted $config"
}

Write-Output "uninstalled $Destination"
Write-Output "kept $userRoot"
