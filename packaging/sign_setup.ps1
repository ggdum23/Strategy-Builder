# Optional Authenticode signing. Off unless SIGN_PFX_PATH is set.
# See docs/INSTALLER.md.

param(
    [Parameter(Mandatory = $true)][string]$SetupExe
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $env:SIGN_PFX_PATH) {
    Write-Output "code signing skipped (SIGN_PFX_PATH is not set)"
    return
}
if (-not (Test-Path -LiteralPath $env:SIGN_PFX_PATH)) {
    throw "SIGN_PFX_PATH does not exist"
}
if (-not $env:SIGN_PFX_PASSWORD) {
    throw "SIGN_PFX_PASSWORD is required when SIGN_PFX_PATH is set"
}

$kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
$signtool = Get-ChildItem -Path $kits -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\signtool.exe$' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $signtool) {
    throw "signtool.exe was not found under $kits"
}

$timestamp = if ($env:SIGN_TIMESTAMP_URL) { $env:SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
& $signtool.FullName sign /fd SHA256 /td SHA256 /tr $timestamp /f $env:SIGN_PFX_PATH /p $env:SIGN_PFX_PASSWORD $SetupExe
if ($LASTEXITCODE -ne 0) {
    throw "signtool failed"
}
Write-Output "signed $SetupExe"
