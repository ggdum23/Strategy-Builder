# Silent per-user install into a directory whose path contains a space.

param(
    [Parameter(Mandatory = $true)][string]$SetupExe,
    [Parameter(Mandatory = $true)][string]$Destination
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Destination -notmatch " ") {
    throw "install directory must contain a space: $Destination"
}
if (-not (Test-Path -LiteralPath $SetupExe)) {
    throw "setup executable not found: $SetupExe"
}

$iss = Get-Content -LiteralPath (Join-Path $PSScriptRoot "strategy_builder.iss") -Raw
$matched = [regex]::Match($iss, '#define DefaultDataRoot "([^"]+)"')
if (-not $matched.Success) {
    throw "DefaultDataRoot is not defined in strategy_builder.iss"
}
$expectedRoot = $matched.Groups[1].Value
$rootExisted = Test-Path -LiteralPath $expectedRoot

$log = Join-Path $env:TEMP "strategy-builder-install.log"
if (Test-Path -LiteralPath $log) {
    Remove-Item -LiteralPath $log -Force
}

Write-Output "Installing to $Destination"
& $SetupExe /VERYSILENT /CURRENTUSER /SUPPRESSMSGBOXES /NORESTART "/DIR=$Destination" "/LOG=$log"
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
    throw "installer exited $LASTEXITCODE"
}

$cli = Join-Path $Destination "backtester.exe"
$gui = Join-Path $Destination "Strategy Builder.exe"
$unins = Join-Path $Destination "unins000.exe"
foreach ($path in @($cli, $gui, $unins, (Join-Path $Destination "defaults.json"), (Join-Path $Destination "README.md"), (Join-Path $Destination "LICENSE.txt"))) {
    if (-not (Test-Path -LiteralPath $path)) {
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
        throw "install did not write $path"
    }
}

$defaults = Get-Content -LiteralPath (Join-Path $Destination "defaults.json") -Raw | ConvertFrom-Json
if ($defaults.data_root -ne $expectedRoot) {
    throw "defaults.json data_root is $($defaults.data_root), expected $expectedRoot"
}

$configPath = Join-Path $env:LOCALAPPDATA "StrategyBuilder\config.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "per-user config was not written: $configPath"
}
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.data_root -ne $expectedRoot) {
    throw "config.json data_root is $($config.data_root), expected $expectedRoot"
}
$runs = Join-Path $env:LOCALAPPDATA "StrategyBuilder\runs"
if (-not (Test-Path -LiteralPath $runs)) {
    throw "runs directory was not created"
}

if (-not $rootExisted -and (Test-Path -LiteralPath $expectedRoot)) {
    throw "installer created the market-data folder $expectedRoot"
}

Write-Output "installed $Destination"
Write-Output "data_root $expectedRoot"
