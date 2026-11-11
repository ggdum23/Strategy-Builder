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
builder on top of the engines. The desktop window is `docs/GUI.md`.
The Windows installer is `docs/INSTALLER.md`. The registry schema is `docs/REGISTRY.md`.
Packs, the sidecar, and the residual leaks are `docs/PACK.md`.

## Install

Python 3.11 or newer. From this directory:

```powershell
py -3 -m pip install -e ".[dev]"
py -3 -m backtester test
py -3 -m pip install -e ".[gui]"
py -3 -m backtester gui
```

On Python 3.11 the GUI extra installs PySide6 6.6 or newer, and not 6.12. PySide6 6.12.0 drops references to `True` while a widget is torn down, and 3.11 still reference-counts that singleton, so the process aborts. Python 3.12 and 3.13 may use 6.12. The Windows installer stays on Python 3.12.

The window is optional. The commands below do not need it. Paths with spaces,
including `C:\Users\Jeu\Desktop\Strategy Builder`, are passed through as one path.

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

`run` writes `metrics.json`, `equity_curve.csv` (also copied to `equity.csv`),
`fills.csv`, `trades.csv`, and `equity.png`. Benchmarks are in `benchmarks/`.
Arena's only benchmark is 1x buy-and-hold through the Arena engine and the Arena
costs. Desk writes that 1x book and `benchmarks/long_2x/`, a constant 2x long
financed at 7% through the Desk engine and the Desk costs. Judge a Desk book on
total return against the 1x book. The leaderboard sorts by that excess total
return, then by strategy id. A higher Sharpe by itself is not the ranking.
Definitions are in `docs/METRICS.md`.

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
split list means the prints are raw. A raw run uses share factor 1.0. Passing
another factor is an error. 0.9 is not a kit constant. An anonymized pack stores
its scale in the operator sidecar, and the runner passes that scale so per-share
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
factor. The sidecar stays on the operator machine. `run --pack` and `parallel`
load that sidecar in the parent and pass `share_scale` into the engine. The
operator `metrics.json` still records that cost basis and the original ticker.
`designer/` is the copy to hand a strategy designer: no ticker, no calendar
date, and no scale field. The designer-facing stand-in for a ticker is
`RANDOM`. Strategy and source directory names use that spelling, with a
`-2`, `-3`, ... suffix when two ids would be the same folder. Do not put
operator paths in a strategy. `referee` still raises, on purpose.
The twenty designer strategies under `examples/strategies/` are the Arena examples.
Desk examples are under `examples/desk/`.

Fixed report names stay canonical: `metrics.json`, `equity_curve.csv`,
`equity.csv`, `equity.png`, `fills.csv`, `trades.csv`, `liquidation.json`,
`error.json`, `leaderboard.csv`, `benchmarks/buy_and_hold_1x`,
`benchmarks/long_2x`, `manifest.json`, `bars_part_NNN.csv.gz`, and
`daily_rth.csv.gz`. An anonymized symbol is rejected when it matches one of
the reserved tokens below (casefolded, as its own token, so `MA` is not
`manifest` and `2` is not `2x`). `pack`, `run --pack`, and `parallel` on an
anonymized pack print the reason. The command exits 2. They do not write the pack or
the report, and they do not print a traceback. Run that symbol as an operator
(raw) book, or rename it in the data. A raw run, including `walkforward`,
keeps the original ticker and is not checked against this list.

Reserved tokens: `001`, `1x`, `2x`, `and`, `arena`, `bars`, `benchmarks`,
`builder`, `buy`, `csv`, `curve`, `daily`, `designer`, `desk`, `equity`,
`error`, `failed`, `fills`, `gz`, `hold`, `json`, `leaderboard`,
`liquidation`, `long`, `manifest`, `margin`, `metrics`, `not`, `part`, `png`,
`post`, `pre`, `published`, `random`, `rth`, `software`, `stopped`,
`strategy`, `tbl`, `the`, `title`, `trades`, `withheld`.

## Commands

```text
python -m backtester run --profile arena|desk --ticker SYM --strategy PATH --src DIR --out DIR
python -m backtester run --profile arena|desk --strategy PATH --pack DIR --sidecar FILE --out DIR
python -m backtester walkforward ... --design-fraction 0.8
python -m backtester ingest --ticker SYM --src DIR --registry FILE.json --profile arena|desk --price-kind raw
python -m backtester registry list --registry FILE.json
python -m backtester registry show --registry FILE.json --ticker SYM
python -m backtester pack --ticker SYM --src DIR --registry FILE.json --out DIR --sidecar FILE (--anonymize | --named)
python -m backtester parallel --strategies DIR --workers N --profile arena|desk --ticker SYM --src DIR --out DIR
python -m backtester parallel --strategies DIR --workers N --profile desk --pack DIR --sidecar FILE --out DIR
python -m backtester report --run RUN_ID
python -m backtester test
```

`parallel` runs one process per job. The worker count does not change a result.
A strategy that crashes, hangs, or exceeds `--memory-mb` is one failed row, and
the rest of the batch still finishes. A process that strategy starts is stopped
with it, including when the strategy returns or raises. `--timeout` starts after that process is up. One `--dividends` file
applies to the whole batch. Arena rejects it. On an anonymized Desk pack the
parent remaps each date and scales the amount before the child starts. The
parent recomputes the statistics and runs the benchmarks; a child that rewrites
those figures fails that row. `report --run` reads a run id from this
directory's `.backtester-runs.json`, or a directory that contains `run.json`.
`--share-factor` is refused on a pack run. The sidecar holds the scale.

`--borrow-rate` overrides that profile's borrow. `--financing-rate` and
`--fee-mult` are Desk switches. Arena rejects a financing rate, a fee multiplier
other than 1, and a dividend file, so the reference loop stays the reference loop.
`--dividends` is a CSV with columns `session,amount`: real dollars per real
share, keyed by calendar date. On an anonymized pack the runner multiplies the
amount by `share_scale` and remaps the date to the pack session id. The child
does not receive the calendar key or the unscaled amount. A date that is not in
the pack is an error.

A pack run prints the cost basis (`share_scale`, schedule, and that per-share
fees used real share counts). `metrics.json` carries the same record.

`python -m backtester test` runs pytest on this checkout. It needs the source tree.

## What this checkout does not do yet

No one-bar referee. The desktop window reads report files and does not score a book itself (`docs/GUI.md`). The parallel runner, the report files, and the equity PNG are in place. The Windows `setup.exe` is built by the installer workflow (`docs/INSTALLER.md`).
No Arena financing switch. The engines, the registry, and the pack builder are importable so those can sit on top without another rewrite. Interpretations that the Desk spec left open are in `docs/DESK_SPEC_DECISIONS.md`. The Arena port is in `docs/ARENA_PORT_NOTES.md`. Walk-forward and parallel isolation are `docs/ISOLATION.md`. Metric definitions are `docs/METRICS.md`.
