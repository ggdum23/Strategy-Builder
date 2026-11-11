# Silent uninstall.
# The first-phase unins000.exe exits before Inno runs usPostUninstall, and that
# callback is what logs "keeping" or "deleting" and what deletes the profile.
# This script waits for the log line, the profile state, and the second-phase
# process (_unins.tmp under a temp is-*-uninstall.tmp directory) before returning.
# A keep result requires the upgrade sentinel to still be in config.json.

param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [switch]$DeleteUserData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "ci_process.ps1")

$sentinel = "D:\strategy-builder-ci-upgrade"
$unins = Join-Path $Destination "unins000.exe"
if (-not (Test-Path -LiteralPath $unins)) {
    throw "uninstaller not found: $unins"
}

$userRoot = Join-Path $env:LOCALAPPDATA "StrategyBuilder"
$config = Join-Path $userRoot "config.json"
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

function Test-UninstallPhaseRunning {
    # The second phase runs as _unins.tmp. That file can outlive the process,
    # so only a live process counts. unins000.exe is the first phase.
    $cim = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $exe = [string]$_.ExecutablePath
        ($name -like 'unins*') -or ($name -eq '_unins.tmp') -or
            ($exe -like '*\_unins.tmp') -or ($exe -like '*\unins*.exe')
    })
    if ($cim.Count -gt 0) {
        return $true
    }
    $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -like 'unins*' -or $_.ProcessName -eq '_unins'
    })
    return $procs.Count -gt 0
}

function Invoke-UninstallGuard {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('keep', 'delete')][string]$Mode
    )
    $guard = Join-Path $PSScriptRoot "uninstall_guard.py"
    $rootFlag = "no"
    if (Test-Path -LiteralPath $userRoot) { $rootFlag = "yes" }
    $PSNativeCommandUseErrorActionPreference = $false
    $output = & python $guard `
        --mode $Mode `
        --sentinel $sentinel `
        --log-file $log `
        --config $config `
        --root-exists $rootFlag | Out-String
    $code = $LASTEXITCODE
    $lines = @($output -split '\r?\n' | Where-Object { $_.Trim() -ne '' })
    $text = if ($lines.Count -gt 0) { $lines[$lines.Count - 1].Trim() } else { '' }
    return @{ Code = [int]$code; Text = $text }
}

function Wait-UninstallSettled {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('keep', 'delete')][string]$Mode
    )
    # usPostUninstall runs after a 500 ms sleep in the second phase. 30 s covers
    # that gap and a slow DelTree. Returning earlier lets the next install start
    # while the profile is still being removed.
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        $result = Invoke-UninstallGuard -Mode $Mode
        if ($result.Code -ne 0) {
            if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
            throw $result.Text
        }
        $idle = -not (Test-UninstallPhaseRunning)
        if ($result.Text -eq 'ok' -and $idle) {
            $again = Invoke-UninstallGuard -Mode $Mode
            if ($again.Code -ne 0) {
                if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
                throw $again.Text
            }
            if ($again.Text -eq 'ok') {
                return
            }
        }
        if ((Get-Date) -ge $deadline) {
            if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
            if ($Mode -eq 'delete') {
                throw "DELETEUSERDATA left $userRoot"
            }
            throw "uninstall keep did not see the keeping line within 30s"
        }
        Start-Sleep -Milliseconds 200
    }
}

$flag = ""
$mode = "keep"
if ($DeleteUserData) {
    $flag = " /DELETEUSERDATA"
    $mode = "delete"
}
$arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART$flag /LOG=""$log"""
$code = Wait-GuiProcess -File $unins -Arguments $arguments
Write-Output "uninstaller exit $code"

$cliDeadline = (Get-Date).AddSeconds(90)
$cli = Join-Path $Destination "backtester.exe"
while ((Test-Path -LiteralPath $cli) -and ((Get-Date) -lt $cliDeadline)) {
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

Wait-UninstallSettled -Mode $mode
if ($DeleteUserData) {
    Write-Output "uninstalled $Destination"
    Write-Output "deleted $userRoot"
    return
}
Write-Output "uninstalled $Destination"
Write-Output "kept $userRoot"
