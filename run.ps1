$ErrorActionPreference = "Stop"
$ArenaFilledExample = "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled"
$DeskFilledExample = "C:\Users\Jeu\Desktop\Backtester\SPY\filled"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python is not on PATH. The installed app will bundle its own runtime; this script uses the checkout."
}
python -m backtester @args
exit $LASTEXITCODE
