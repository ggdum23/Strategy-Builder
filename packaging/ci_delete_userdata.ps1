# Reinstall, then uninstall with /DELETEUSERDATA. The market-data folder stays.

param(
    [Parameter(Mandatory = $true)][string]$SetupExe,
    [Parameter(Mandatory = $true)][string]$Destination
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ci_process.ps1")

if ($Destination -notmatch " ") {
    throw "install directory must contain a space: $Destination"
}

$userRoot = Join-Path $env:LOCALAPPDATA "StrategyBuilder"
$configPath = Join-Path $userRoot "config.json"
$upgradeMarker = Join-Path $userRoot "ci-upgrade.txt"
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "keep-uninstall removed $configPath"
}
if (-not (Test-Path -LiteralPath $upgradeMarker)) {
    throw "keep-uninstall removed $upgradeMarker"
}

$iss = Get-Content -LiteralPath (Join-Path $PSScriptRoot "strategy_builder.iss") -Raw
$matched = [regex]::Match($iss, '#define DefaultDataRoot "([^"]+)"')
if (-not $matched.Success) {
    throw "DefaultDataRoot is not defined in strategy_builder.iss"
}
$defaultRoot = $matched.Groups[1].Value
$sentinel = "D:\strategy-builder-ci-upgrade"
$defaultExisted = Test-Path -LiteralPath $defaultRoot

$log = Join-Path $env:TEMP "strategy-builder-reinstall.log"
if (Test-Path -LiteralPath $log) {
    Remove-Item -LiteralPath $log -Force
}
Write-Output "Reinstalling $Destination before DELETEUSERDATA"
$arguments = "/VERYSILENT /CURRENTUSER /SUPPRESSMSGBOXES /NORESTART /DIR=""$Destination"" /LOG=""$log"""
$code = Wait-GuiProcess -File $SetupExe -Arguments $arguments
Write-Output "reinstall exit $code"
if ($code -ne 0) {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
    throw "reinstall exited $code"
}

$sentinelFile = Join-Path $sentinel "ci-sentinel-keep.txt"
New-Item -ItemType Directory -Force -Path $sentinel | Out-Null
Set-Content -LiteralPath $sentinelFile -Value "survive" -Encoding ascii
$tempRoot = Join-Path $env:TEMP "strategy-builder-data-root"
$tempFile = Join-Path $tempRoot "ci-data-root-keep.txt"
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
Set-Content -LiteralPath $tempFile -Value "survive" -Encoding ascii

& (Join-Path $PSScriptRoot "ci_uninstall.ps1") -Destination $Destination -DeleteUserData
if (Test-Path -LiteralPath $userRoot) {
    throw "user data still exists: $userRoot"
}
if (-not $defaultExisted -and (Test-Path -LiteralPath $defaultRoot)) {
    throw "DELETEUSERDATA created the market-data folder $defaultRoot"
}
foreach ($planted in @($sentinelFile, $tempFile)) {
    if (-not (Test-Path -LiteralPath $planted)) {
        throw "DELETEUSERDATA removed $planted"
    }
    if ((Get-Content -LiteralPath $planted -Raw).Trim() -ne "survive") {
        throw "DELETEUSERDATA changed $planted"
    }
}

Write-Output "deleted user data"
Write-Output "kept market data"
