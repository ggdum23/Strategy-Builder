$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python is not on PATH. The installed app will bundle its own runtime; this script uses the checkout."
}
python -m backtester @args
exit $LASTEXITCODE
