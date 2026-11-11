# Build the one-folder bundle and the Inno Setup installer.
# Run on Windows from any working directory. Requires the repo, Python 3.12,
# and PyInstaller 6.22.3. The compiler must be Inno Setup 6.7.4. Any other
# ISCC.exe is ignored. A missing 6.7.4 is downloaded and hash-checked first.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$PyInstallerPin = "6.22.3"
$InnoVersion = "6.7.4"
$InnoUrl = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-$InnoVersion.exe"
# SHA256 of the official GitHub release asset innosetup-6.7.4.exe (10,604,576 bytes).
$InnoSha256 = "a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c"

. (Join-Path $PSScriptRoot "ci_process.ps1")

function Get-LeadingVersion {
    param([string]$Text)
    if ($Text -match '(\d+\.\d+\.\d+(?:\.\d+)?)') {
        return $Matches[1]
    }
    return ""
}

function Get-IsccVersionText {
    param([Parameter(Mandatory = $true)][string]$Path)
    $info = (Get-Item -LiteralPath $Path).VersionInfo
    $product = Get-LeadingVersion ([string]$info.ProductVersion)
    if ($product) { return $product }
    return Get-LeadingVersion ([string]$info.FileVersion)
}

function Test-ExactInnoVersion {
    param([Parameter(Mandatory = $true)][string]$Text)
    $parts = @($Text.Split('.') | Where-Object { $_ -ne '' })
    if ($parts.Count -lt 3) { return $false }
    return ($parts[0] -eq '6' -and $parts[1] -eq '7' -and $parts[2] -eq '4')
}

function Install-InnoSetup {
    $installer = Join-Path $env:TEMP "innosetup-$InnoVersion.exe"
    $dest = Join-Path $Root "packaging\build\innosetup-$InnoVersion"
    Write-Host "Downloading Inno Setup $InnoVersion"
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $InnoUrl -OutFile $installer
    Unblock-File -LiteralPath $installer
    $got = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($got -ne $InnoSha256) {
        Remove-Item -LiteralPath $installer -Force
        throw "Inno Setup download hash is $got, expected $InnoSha256"
    }
    Write-Host "Inno Setup download hash ok"
    if (Test-Path -LiteralPath $dest) {
        Remove-Item -LiteralPath $dest -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    # Verify the hash before this process starts. A GUI installer does not set $LASTEXITCODE.
    $arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR=""$dest"""
    $code = Wait-GuiProcess -File $installer -Arguments $arguments
    if (($code -ne 0) -and ($code -ne 3010)) {
        throw "Inno Setup installer exited $code"
    }
    $found = Get-ChildItem -LiteralPath $dest -Filter ISCC.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($found) {
        return $found.FullName
    }
    # Some Inno installers ignore /DIR and upgrade the copy already on the machine.
    foreach ($fallback in @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )) {
        if (-not (Test-Path -LiteralPath $fallback)) { continue }
        if (Test-ExactInnoVersion (Get-IsccVersionText $fallback)) {
            return $fallback
        }
    }
    throw "ISCC.exe was not installed to $dest"
}

function Resolve-Iscc {
    $candidates = @(
        (Join-Path $Root "packaging\build\innosetup-$InnoVersion\ISCC.exe"),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        $version = Get-IsccVersionText $candidate
        if (Test-ExactInnoVersion $version) {
            return $candidate
        }
        Write-Host "Ignoring $candidate (version $version, required $InnoVersion)"
    }
    return Install-InnoSetup
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

$iscc = Resolve-Iscc
if (-not $iscc -or -not (Test-Path -LiteralPath $iscc)) {
    throw "ISCC.exe not found after installing Inno Setup $InnoVersion"
}
$isccVersion = Get-IsccVersionText $iscc
if (-not (Test-ExactInnoVersion $isccVersion)) {
    throw "ISCC $iscc is version $isccVersion; this build requires $InnoVersion"
}
Write-Output "ISCC $iscc version $isccVersion"

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
