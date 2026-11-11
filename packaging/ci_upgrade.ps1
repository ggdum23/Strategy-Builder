# Install over an existing per-user install. config.json must be left as the user edited it.

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
if (-not (Test-Path -LiteralPath (Join-Path $Destination "backtester.exe"))) {
    throw "upgrade requires an existing install: $Destination"
}

$sentinel = "D:\strategy-builder-ci-upgrade"
$userRoot = Join-Path $env:LOCALAPPDATA "StrategyBuilder"
$configPath = Join-Path $userRoot "config.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "upgrade requires $configPath"
}
$json = Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json
$json.data_root = $sentinel
if ($json.PSObject.Properties.Name -contains "ci_marker") {
    $json.ci_marker = "upgrade-keep"
} else {
    $json | Add-Member -NotePropertyName ci_marker -NotePropertyValue "upgrade-keep"
}
($json | ConvertTo-Json -Depth 5) + "`n" | Set-Content -LiteralPath $configPath -Encoding utf8
$marker = Join-Path $userRoot "ci-upgrade.txt"
Set-Content -LiteralPath $marker -Value "upgrade-keep" -Encoding ascii

$log = Join-Path $env:TEMP "strategy-builder-upgrade.log"
if (Test-Path -LiteralPath $log) {
    Remove-Item -LiteralPath $log -Force
}
Write-Output "Upgrading $Destination"
$arguments = "/VERYSILENT /CURRENTUSER /SUPPRESSMSGBOXES /NORESTART /DIR=""$Destination"" /LOG=""$log"""
$code = Wait-GuiProcess -File $SetupExe -Arguments $arguments
Write-Output "upgrade exit $code"
if ($code -ne 0) {
    if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
    throw "upgrade exited $code"
}

$after = Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($after.data_root -ne $sentinel) {
    throw "upgrade rewrote data_root to $($after.data_root)"
}
if ($after.ci_marker -ne "upgrade-keep") {
    throw "upgrade dropped ci_marker"
}
if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne "upgrade-keep") {
    throw "upgrade removed $marker"
}
foreach ($name in @("backtester.exe", "Strategy Builder.exe", "unins000.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $Destination $name))) {
        throw "upgrade removed $name"
    }
}
if (Test-Path -LiteralPath $sentinel) {
    throw "upgrade created the sentinel data folder $sentinel"
}

Write-Output "upgrade preserved config.json"
Write-Output "data_root $sentinel"
