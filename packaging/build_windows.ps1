# Build the one-folder bundle and the Inno Setup installer.
# Run on Windows from any working directory. Requires the repo, Python 3.12,
# and PyInstaller 6.22.3. Installs Inno Setup 6.7.4 when ISCC.exe is missing.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$PyInstallerPin = "6.22.3"
$InnoVersion = "6.7.4"
$InnoUrl = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-$InnoVersion.exe"

function Find-Iscc {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

function Install-InnoSetup {
    $installer = Join-Path $env:TEMP "innosetup-$InnoVersion.exe"
    Write-Output "Downloading Inno Setup $InnoVersion"
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $InnoUrl -OutFile $installer
    Unblock-File -LiteralPath $installer
    & $installer /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-
    # 3010 means the installer asked for a reboot. The compiler is usable without one.
    if (($LASTEXITCODE -ne 0) -and ($LASTEXITCODE -ne 3010)) {
        throw "Inno Setup installer exited $LASTEXITCODE"
    }
}

& $Python (Join-Path $Root "packaging\versions.py") --check
if ($LASTEXITCODE -ne 0) { throw "version check failed" }

$project = (& $Python (Join-Path $Root "packaging\versions.py") --print project).Trim()
$innoVersion = (& $Python (Join-Path $Root "packaging\versions.py") --print inno).Trim()
$versionInfo = (& $Python (Join-Path $Root "packaging\versions.py") --print version-info).Trim()
$pyi = (& $Python -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
if ($pyi -ne $PyInstallerPin) {
    throw "PyInstaller $pyi is installed; this build uses $PyInstallerPin"
}

$guiExtra = (& $Python (Join-Path $Root "packaging\versions.py") --has-extra gui).Trim()
if ($guiExtra -eq "yes") {
    Write-Output "Installing the gui extra"
    & $Python -m pip install ".[gui]"
    if ($LASTEXITCODE -ne 0) { throw "pip install .[gui] failed" }
}

$epoch = (& git -C $Root log -1 --format=%ct).Trim()
if (-not $epoch) {
    throw "SOURCE_DATE_EPOCH needs the current commit timestamp"
}
$env:SOURCE_DATE_EPOCH = $epoch
$env:PYTHONHASHSEED = "0"
$env:PYTHONDONTWRITEBYTECODE = "1"

& $Python (Join-Path $Root "packaging\make_icon.py")
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

$dist = Join-Path $Root "packaging\dist"
$work = Join-Path $Root "packaging\build\pyinstaller"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

Write-Output "PyInstaller $pyi SOURCE_DATE_EPOCH=$epoch"
& $Python -m PyInstaller --noconfirm --clean `
    --distpath $dist `
    --workpath $work `
    (Join-Path $Root "packaging\strategy_builder.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$bundle = Join-Path $dist "StrategyBuilder"
& $Python (Join-Path $Root "packaging\verify_bundle.py") $bundle
if ($LASTEXITCODE -ne 0) { throw "bundle verification failed" }

$iscc = Find-Iscc
if (-not $iscc) {
    Install-InnoSetup
    $iscc = Find-Iscc
}
if (-not $iscc) {
    throw "ISCC.exe not found after installing Inno Setup $InnoVersion"
}
Write-Output "ISCC $iscc"

& $iscc `
    "/DAppVersion=$innoVersion" `
    "/DVersionInfoVersion=$versionInfo" `
    "/DSetupName=$project" `
    "/DPayloadDir=$bundle" `
    "/DRepoRoot=$Root" `
    "/DOutputDir=$dist" `
    (Join-Path $Root "packaging\strategy_builder.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$setup = Join-Path $dist "StrategyBuilder-Setup-$project.exe"
if (-not (Test-Path -LiteralPath $setup)) {
    throw "setup executable was not written: $setup"
}

& (Join-Path $Root "packaging\sign_setup.ps1") -SetupExe $setup
if ($LASTEXITCODE -ne 0) { throw "signing step failed" }

$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
$size = (Get-Item -LiteralPath $setup).Length
$sums = Join-Path $dist "SHA256SUMS"
$set = "{0}  {1}" -f $hash, (Split-Path -Leaf $setup)
Set-Content -LiteralPath $sums -Value $set -Encoding ascii
Write-Output "SHA256 $hash"
Write-Output "SIZE $size"
Write-Output "SETUP $setup"

if ($env:GITHUB_STEP_SUMMARY) {
    @(
        "### Installer",
        "",
        "- File: ``$(Split-Path -Leaf $setup)``",
        "- Size: $size bytes",
        "- SHA256: ``$hash``",
        "- PyInstaller: $pyi",
        "- Inno Setup: $InnoVersion",
        "- Python: $(& $Python -c 'import sys; print(sys.version.split()[0])')",
        "- SOURCE_DATE_EPOCH: $epoch"
    ) | Out-File -FilePath $env:GITHUB_STEP_SUMMARY -Append -Encoding utf8
}
