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

Phase 1 is the engines, the named-file loader, and the CLI. The desktop window and
the Windows installer are not built yet. See `packaging/README.md`.

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
or one date into the next.

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

Inspect a tree before trusting it:

```powershell
.\run.ps1 ingest --ticker QQQ `
  --src "C:\Users\Jeu\Desktop\Backtester\1min_Candles-20261107T193139Z-1-001\1min_Candles\QQQ\filled" `
  --out .\out\qqq_inspect.json
```

That writes a description and does not copy the files. A new symbol needs its own
filled files, its own dividend file if Desk should pay them, and an explicit split
decision. An empty split list means the prints are raw. A hidden scale factor is
not applied. `--share-factor 0.9` is only for an anonymized price scale; on these
raw files it warns, because per-share fees would be wrong.

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

**Blind designers.** A later pack builder anonymizes a series, and a later
referee sends one bar at a time. Those commands (`pack`, `parallel`, `referee`)
raise, on purpose. Do not put operator paths in a strategy. The twenty designer
strategies under `examples/strategies/` are the Arena examples from that work.
Desk examples are under `examples/desk/`.

## Commands

```text
python -m backtester run --profile arena|desk --ticker SYM --strategy PATH --src DIR --out DIR
python -m backtester walkforward ... --design-fraction 0.8
python -m backtester ingest --ticker SYM --src DIR --out FILE.json
python -m backtester test
```

`--borrow-rate` overrides that profile's borrow. `--financing-rate` and
`--fee-mult` are Desk switches. Arena rejects a financing rate, a fee multiplier
other than 1, and a dividend file, so the reference loop stays the reference loop.
`--dividends` is a CSV with columns `session,amount`.

`python -m backtester test` runs pytest on this checkout. It needs the source tree.

## What this phase does not do

No window, no `setup.exe`, no one-bar referee, no process pool, no anonymized
pack builder, no equity PNG, no Arena financing switch. The engines are importable
so those can sit on top without another rewrite. Interpretations that the Desk
spec left open are in `docs/DESK_SPEC_DECISIONS.md`. The Arena port is in
`docs/ARENA_PORT_NOTES.md`.
