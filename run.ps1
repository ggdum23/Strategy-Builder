# Run the backtester from this checkout. Does not copy, move, or unzip market data.
# Point --src at the filled directory that is already on disk.
#
# QQQ filled tree (Arena example):
#   C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled
# SPY filled tree (Desk example):
#   C:\Users\Jeu\Desktop\Backtester\SPY\filled
#
#   .\run.ps1 run --profile arena --ticker QQQ --strategy .\examples\strategies\D02\strategy.py `
#     --src "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled" `
#     --out .\out\d02
#   .\run.ps1 run --profile desk --ticker SPY --strategy .\examples\desk\buy_and_hold.py `
#     --src "C:\Users\Jeu\Desktop\Backtester\SPY\filled" `
#     --out .\out\spy_desk
#   .\run.ps1 ingest --ticker QQQ `
#     --src "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled" `
#     --registry .\registry.json --profile arena --price-kind raw
#   .\run.ps1 registry show --registry .\registry.json --ticker QQQ

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python is not on PATH. The installed app will bundle its own runtime; this script uses the checkout."
}
python -m backtester @args
exit $LASTEXITCODE
