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
operator paths in a strategy. A strategy file passed to `run`, `run --pack`,
`walkforward`, `parallel`, or `referee` executes only in the OS jail. The
operator process does not import it. The child does not receive the sidecar,
the calendar, or `share_scale`. An in-memory class passed to the library stays
in process. `--unsafe-in-process` is the debug escape: it prints a warning and
refuses a pack.
The twenty designer strategies under `examples/strategies/` are the Arena examples.
Desk examples are under `examples/desk/`.

Fixed report names stay canonical: `metrics.json`, `equity_curve.csv`,
`equity.csv`, `equity.png`, `fills.csv`, `trades.csv`, `liquidation.json`,
`error.json`, `leaderboard.csv`, `benchmarks/buy_and_hold_1x`,
`benchmarks/long_2x`, `manifest.json`, `bars_part_NNN.csv.gz`,
`daily_rth.csv.gz`, and `referee.json`. An anonymized symbol is rejected when it matches one of
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
`post`, `pre`, `published`, `random`, `referee`, `rth`, `software`, `stopped`,
`strategy`, `tbl`, `the`, `title`, `trades`, `withheld`.

These accounting values and keys are reserved as the whole symbol, and are
not split on `_` (`session_boundary_1_252` does not reserve `1`):
`accounting`, `accrual`, `cash_dividends`, `cash_interest`,
`cash_rate_annual`, `dividends_applied`, `dividends_net`, `financing_cost`,
`financing_rate_annual`, `none`, `price_basis`, `profile_dividends`, `raw`,
`registry_series`, `series_kind`, `session_boundary_1_252`,
`sessions_per_year`, `total_return`, `total_return_method`,
`withholding_rate`.

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
python -m backtester referee --strategy PATH --hidden PATH --profile arena|desk --out DIR
python -m backtester test
```

`parallel` runs one process per job. The worker count does not change a result.
A strategy that crashes, hangs, or exceeds `--memory-mb` is one failed row, and
the rest of the batch still finishes. A process that strategy starts is stopped
with it, including when the strategy returns or raises. `--timeout` starts after that process is up. One `--dividends` file
applies to the whole batch. Arena credits that file only with `--price-basis total_return`. On an anonymized pack the
parent remaps each date and scales the amount before the child starts. The
parent recomputes the statistics and runs the benchmarks; a child that rewrites
those figures fails that row. `report --run` reads a run id from this
directory's `.backtester-runs.json`, or a directory that contains `run.json`.
`--share-factor` is refused on a pack run. The sidecar holds the scale.

`--borrow-rate` overrides that profile's borrow. `--fee-mult` is a Desk
switch. Arena rejects a fee multiplier other than 1. `--financing-rate` and
`--cash-rate` are annual decimals (0.07 means 7 percent). Omit them and Arena
finances at 0 while Desk finances at 0.07; cash earns 0 on both. The default
call does not change the reference Arena loop.
`--dividends` is a CSV with columns `session,amount`: real dollars per real
share, keyed by calendar date. On an anonymized pack the runner multiplies the
amount by `share_scale` and remaps the date to the pack session id. The child
does not receive the calendar key or the unscaled amount. A date that is not in
the pack is an error. See the price-basis section below.

A pack run prints the cost basis (`share_scale`, schedule, and that per-share
fees used real share counts). `metrics.json` carries the same record.

`python -m backtester test` runs pytest on this checkout. It needs the source tree.

## Price basis, cash, and financing

These switches are opt-in. Leaving them off reproduces the previous books.

| Switch | Default | What it does |
| --- | --- | --- |
| `--price-basis raw` | yes | Use the prints in the file. Arena still refuses a dividend file. Desk credits a dividend file gross (`profile_dividends`) |
| `--price-basis total_return` | no | Either run a registry series declared `total_return` (`registry_series`), or credit `dividends.csv` on a raw series (`cash_dividends`) |
| `--cash-rate` | 0 | Idle cash earns `rate / 252` once per session boundary |
| `--financing-rate` | Arena 0, Desk 0.07 | Borrowed money is charged `rate / 252` once per session boundary. Desk's omitted rate is `costs_desk.FINANCING_RATE_ANNUAL`, read when that run starts |
| `--withholding` | 0 | Fraction kept back from a positive long dividend. Shorts pay the gross amount |

Idle cash is `max(0, equity - |shares * price|)`. Borrowed money is `max(0, |shares * price| - equity)`. Both use the previous session's snap, before the charge. They are not both positive. The charge is applied at the next session boundary, before that session's first fill. There is no calendar-day count. A rate below 0 or above 1.0 is an error. `0.07` means 7 percent per year.

The snap is the book after the session's last bar, after that bar's fill. Arena borrow stays on the last regular bar. When the strategy receives post-session bars, a fill there is in the snap. A warm-up reset clears the snap and the accrued totals before the first live session, so N live sessions pay N-1 charges and the first live mark is the reset equity. Desk already clears its snap in `enter_live`. A Desk target on a post bar fills at the next regular open, after the boundary charge.

A long dividend with withholding `w` credits `shares * amount * (1 - w)` when the amount is positive. Withholding 0 uses `shares * amount` for longs and shorts. A short always pays the gross `shares * amount`.

Walk-forward resolves one accounting record from the full dividend file, then cuts that file in the parent before each child starts. The design child receives only ex-dates inside the design frame. The locked child receives only ex-dates inside its frame, warm-up included. A window with no ex-date still keeps that record: the method and `dividends_applied` come from the full file, and the cash credit is zero. A dividend date that is not in the full sample fails the walk-forward for Arena and for Desk, with `dividend date … is not in this sample`, before either child starts. An Arena `run` of a complete sample still rejects a missing date. A Desk `run` still looks a session up and credits nothing when the key is absent. Arena raw plus a dividend file is still `The arena profile does not credit dividends.`

A series ingested as `total_return` stays `runnable: false` in the registry, and a pack of that series is still refused. `run --price-basis total_return --registry registry.json` executes it when the entry has no declared splits. Prices are not rewritten. A dividend file on that run is an error, because the series already includes distributions. Without `--registry`, `--price-basis total_return` means cash dividends and requires the CSV.

Every run writes `price_basis`, `total_return_method`, `series_kind`, `cash_rate_annual`, `financing_rate_annual`, `withholding_rate`, `accrual` (`session_boundary_1_252`), `sessions_per_year` (252), and `dividends_applied` on `metrics.json` and `run.json`, both flat and under `accounting`. The leaderboard has the same columns and refuses a file whose rows disagree. A pack manifest and its sidecar record `price_basis` as the series kind (`raw` or `total_return`).

Raw runs keep `share_factor` 1.0. An anonymized pack still passes the sidecar `share_scale` so per-share fees use real share counts. Value-based charges stay on trade value. Designer files still replace the ticker with `RANDOM` and do not carry the scale.

## What this checkout does not do yet

The desktop window reads report files and does not score a book itself (`docs/GUI.md`). The one-bar referee is `python -m backtester referee`. The parallel runner, the report files, and the equity PNG are in place. The Windows `setup.exe` is built by the installer workflow. See `docs/INSTALLER.md`.
The engines, the registry, and the pack builder stay importable. Interpretations that the Desk spec left open are in
`docs/DESK_SPEC_DECISIONS.md`. The Arena port is in `docs/ARENA_PORT_NOTES.md`.
Walk-forward and parallel isolation are `docs/ISOLATION.md`. Metric
definitions are `docs/METRICS.md`.

## Referee

`referee` replays a hidden sample one bar per pipe round trip. Bar t+1 is
written only after the decision for bar t. The per-call limit is 10 seconds.
A strategy that reads ahead blocks until that limit and is disqualified. A
crash, a non-finite target, and a protocol break are disqualified too. The
process exit is 1. The report is `error.json` plus `referee.json`.

The official engine stays in the parent. Arena fills at the next delivered
bar's open. Desk fills at the next regular-session open. Fees use the real
calendar month and real share counts (`share_scale` from the sidecar, or 1.0
on named files). Neither the scale nor the calendar is sent to the child.
`--share-factor` is refused on a pack. A named run that passes a factor other
than 1.0 exits 2.

Warm-up follows the profile that is running. Arena feeds about the last 504
train sessions through the same engine, then rescales the account to
$1,000,000 and keeps the exposure at the first hidden bar. Desk runs the
whole train history with `live` false: targets are ignored, the book stays
flat, and indicators still update. It then goes live at $1,000,000 on the
hidden bars. `--warmup-days` is the Arena session count. Desk rejects it.

The child runs in an OS jail. Windows uses a job object (kill on close, one
active process, a memory cap, a CPU cap), an AppContainer with no network
capability, and a deny ACE on the data, market, and repository directories.
Linux uses bubblewrap when `bwrap` can start a namespace, and otherwise
landlock plus seccomp. If the jail cannot be applied, the command exits 2 and
the strategy is not started. The guarantees that were actually applied are
written to `referee.json`. `docs/ISOLATION.md` lists them per operating system.
