# Strategy Builder

Local backtester for 1-minute-bar strategies. Two engines, kept apart:

| | Arena | Desk |
| --- | --- | --- |
| Fill | next delivered bar (RTH only, unless the strategy sets extended hours) | next regular-session open only |
| Leverage | clamp to ±2 | clamp to ±3, margin call above 3.15 back to ±3 |
| Borrow | 0.30% / 252 at the regular close | 0.25% / 252 at the session boundary |
| Financing | 0 | 7% / 252 on `max(0, \|position value\| − equity)` |
| Dividends | none | cash before the ex session's first bar |
| Liquidation | none | equity below 30% of the running high-water mark flattens and flags the run |
| Warm-up | trade, then rescale to the starting equity and keep exposure | stay flat (`live` is false), then start flat at the starting equity |
| Fees | `costs.py` (reference schedule, including its TAF) | `costs_desk.py` (same tier walk, different SEC and TAF) |

Starting capital is $1,000,000. Share counts are whole shares, truncated toward zero.
The same target is not rebalanced for drift. Costs, splits, data roots, share factor,
and borrow are configuration. The two fee schedules are not.

Phase 2 adds the series registry, session-anchored resampling, and the pack
builder on top of the engines. The desktop window and the Windows installer are
not built yet. See `packaging/README.md`. The registry schema is `docs/REGISTRY.md`.
Packs, the sidecar, and the residual leaks are `docs/PACK.md`.

## Install

Python 3.11 or newer. From this directory:

```powershell
py -3 -m pip install -e ".[dev]"
py -3 -m backtester test
```

`run.ps1` and `run.bat` call `python -m backtester` and do not copy or unzip data.

## Named local data

Files are `<SYM>_1min_filled_YYYY-MM.csv.gz` with columns
`timestamp_et,epoch_utc,open,high,low,close,volume,wap,count,filled,session`
(`pre`, `rth`, `post`). Prices stay raw. Share factor is 1.0. A non-empty split
list is rejected rather than applied on the quiet.

`--bar-minutes N` rebuilds bars inside each session segment, anchored at that
segment's first minute. A bar never crosses pre into regular, or regular into post,
or one date into the next. A trailing bucket that does not contain `N` contiguous
minutes is kept and marked `partial`. It is not padded and it does not borrow
minutes from the next segment. Session hours are inferred from the stamps and
written into the registry. A tape that trades 08:00–14:00 stays 08:00–14:00.

On the Windows machine the filled trees already on disk are:

```text
C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\<SYM>\filled\
C:\Users\Jeu\Desktop\Backtester\SPY\filled\
```

`<SYM>` is one of AAPL, ABBV, AMD, BRK.B, CVX, GOOG, GOOGL, INTC, JNJ, JPM, LLY,
MA, META, MSFT, MU, NVDA, PLTR, QQQ, SPY, TSLA, V, WMT, XOM. `BRK.B` is a legal
symbol in the file name. This repo does not contain those trees. It contains one
month, October 2026, for QQQ and SPY, under `assets/sample/`.

```powershell
.\run.ps1 run --profile arena --ticker QQQ `
  --strategy .\examples\strategies\D02\strategy.py `
  --src "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled" `
  --out .\out\d02

.\run.ps1 run --profile desk --ticker SPY `
  --strategy .\examples\desk\buy_and_hold.py `
  --src "C:\Users\Jeu\Desktop\Backtester\SPY\filled" `
  --out .\out\spy
```

`run` writes `metrics.json`, `equity_curve.csv`, `fills.csv`, and `trades.csv`.
Benchmarks are in `benchmarks/`. Arena's benchmark is 100% long through the Arena
engine and costs. Desk also writes `benchmarks/long_2x/`, a constant 2x long
financed at 7% through the Desk engine. Judge a Desk book on total return against
the 1x book. A higher Sharpe by itself is not the ranking.

Inspect a tree before trusting it, and record the series when you want to run it
again. Adding a ticker is a registry entry plus an ingest. It is not a code change.

```powershell
.\run.ps1 ingest --ticker QQQ `
  --src "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled" `
  --registry .\registry.json --profile arena --price-kind raw

.\run.ps1 registry list --registry .\registry.json
.\run.ps1 registry show --registry .\registry.json --ticker QQQ
```

That does not copy the files. A new symbol needs its own filled files, its own
dividend file if Desk should pay them, and an explicit split decision. An empty
split list means the prints are raw. `--share-factor 0.9` on these raw files
warns, because per-share fees would be wrong. An anonymized pack stores its own
factor in the operator sidecar, and the runner passes that factor so per-share
fees still use real share counts.

## Walk-forward

There is no default cut. Pass one of `--design-end-day`, `--design-end-date`, or
`--design-fraction`. The design run never sees a locked bar. `--warmup-days` is
how many sessions before the locked tail are replayed, and the profile decides
what that replay does. The QQQ pack's 0..2999 / 3000..3795 windows stay on the
Arena pack runner only. They are not the default here.

```powershell
.\run.ps1 walkforward --profile arena --ticker QQQ `
  --strategy .\examples\strategies\D02\strategy.py `
  --src "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled" `
  --design-end-date 2024-12-31 --warmup-days 21 `
  --out .\out\d02_walk
```

## Two ways to use it

**Named local.** Real dates, raw prices, a ticker, your own files. This is the
CLI above. Reports are yours.

**Blind designers.** `pack --anonymize` writes a chunked pack and an operator
sidecar. The pack has no ticker, no real year in a file name, and no scale
factor. The sidecar stays on the operator machine. `run --pack` loads that
sidecar itself and passes `share_scale` into the engine. Do not put operator
paths in a strategy. `parallel` and `referee` still raise, on purpose. The
twenty designer strategies under `examples/strategies/` are the Arena examples.
Desk examples are under `examples/desk/`.

## Commands

```text
python -m backtester run --profile arena|desk --ticker SYM --strategy PATH --src DIR --out DIR
python -m backtester run --profile arena|desk --strategy PATH --pack DIR --sidecar FILE --out DIR
python -m backtester walkforward ... --design-fraction 0.8
python -m backtester ingest --ticker SYM --src DIR --registry FILE.json --profile arena|desk --price-kind raw
python -m backtester registry list --registry FILE.json
python -m backtester registry show --registry FILE.json --ticker SYM
python -m backtester pack --ticker SYM --src DIR --registry FILE.json --out DIR --sidecar FILE (--anonymize | --named)
python -m backtester test
```

`--borrow-rate` overrides that profile's borrow. `--financing-rate` and
`--fee-mult` are Desk switches. Arena rejects a financing rate, a fee multiplier
other than 1, and a dividend file, so the reference loop stays the reference loop.
`--dividends` is a CSV with columns `session,amount`. An anonymized pack refuses
a dividend file: pack share counts are rescaled, and a raw per-share dividend
would be applied to the wrong count.

A pack run prints the cost basis (`share_scale`, schedule, and that per-share
fees used real share counts). `metrics.json` carries the same record.

`python -m backtester test` runs pytest on this checkout. It needs the source tree.

## What this phase does not do

No window, no `setup.exe`, no one-bar referee, no process pool, no equity PNG,
no Arena financing switch. The engines, the registry, and the pack builder are
importable so those can sit on top without another rewrite. Interpretations that
the Desk spec left open are in `docs/DESK_SPEC_DECISIONS.md`. The Arena port is
in `docs/ARENA_PORT_NOTES.md`. Walk-forward isolation is `docs/ISOLATION.md`.
