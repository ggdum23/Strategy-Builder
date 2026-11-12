# Desktop research terminal

`py -3 -m backtester gui` opens the window. It is a PySide6 application. The engines, the registry, the pack builder, and the runner are unchanged. The window calls those public functions and then reads the files they wrote.

Install the extra. The core CLI does not need it.

```powershell
py -3 -m pip install -e ".[gui]"
py -3 -m backtester gui
```

On Python 3.11 that extra installs PySide6 6.6 or newer, and not 6.12. PySide6 6.12.0 aborts a 3.11 process during widget teardown. Python 3.12 and 3.13 may use 6.12. The Windows installer stays on Python 3.12.

The same entry is `backtester.desktop:main`, which a frozen executable can call. Qt is imported only when that function runs.

## What it is for

Six pages, one path through a study:

1. **Data.** Point at a folder. The default root shown in the field is `C:\Users\Jeu\Desktop\Backtester`. Inspect and, if you choose, record the series. Hours, tick, timezone, fill policy, gaps, and warnings are the inspection result. A gap or a non-runnable series is a banner, not a footnote.
2. **Packs.** Build a named pack or an anonymized pack. The choice is a radio, not a default. The sidecar path is shown. The sidecar is not opened. The manifest on screen is the designer-facing manifest only (`RANDOM`, chunks, bar size, pack id).
3. **Strategies.** Pick a `strategy.py` or a folder of them. The list is an AST parse. Nothing is imported in the window process. Arena and Desk rules (clamp, fills, fees, borrow, financing, liquidation, warm-up) are written out from `backtester.profiles`, with the numbers taken from that profile.
4. **Run.** Single, walk-forward, or batch. The summary form is what will be passed: profile, paths, equity, and a blank rate named as the profile default rather than a substituted number. Walk-forward requires exactly one cut. There is no default cut. Run is disabled, and drawn as disabled, until the draft validates. The engine runs in a spawned process. Cancel terminates that process. The window thread does not call `on_bar`.
5. **Results.** Seven headline figures, the equity curve with the benchmark and a linked drawdown picture underneath, then activity, benchmarks (including Desk financing), fees, period returns, and the leaderboard. Fills, round trips, the saved `equity.png` if it is there, and the Desk risk panel sit on their own tabs. Every figure is read from the run directory. An anonymized book opens as a designer view.
6. **History.** Folders under a runs root that contain `metrics.json` or `walkforward.json`.

Paths are `pathlib` paths. `C:\Users\Jeu\Desktop\Strategy Builder` is one path. It is not split on the space.

The theme is dark or light, Fusion, with a fixed type scale. Qt 6 high-DPI rounding is pass-through. On Windows the process id is `StrategyBuilder.Backtester` so the taskbar icon groups. The UI font is Segoe UI when it is installed, otherwise Inter, Source Sans 3, IBM Plex Sans, Noto Sans, or DejaVu Sans. Numbers use Cascadia Mono, Cascadia Code, JetBrains Mono, Consolas, or DejaVu Sans Mono.

## What the window refuses to do

- It does not recompute Sharpe, drawdown, fees, or a rank. A dash means the report did not contain the field. A zero is shown only when the file stored a zero.
- It does not pick Arena or Desk, raw or total-return, or a walk-forward cut when the operator has not.
- It does not offer a share factor of 0.9. Named runs do not pass a share factor. The runner keeps raw files at 1. Pack runs read the sidecar inside the runner.
- It does not display sidecar contents: price factor, volume factor, date map, operator note. If a report file contains one of those keys, the key is withheld and an operator page says so.
- A designer view never shows the ticker, calendar dates, the invoice scale, or raw prices. An anonymized book opens in that view. The operator can switch to the operator ledger on purpose. Named runs stay on the operator view, where the invoice scale is shown.
- It does not write `leaderboard.csv`. Phase 3 owns that file. A batch calls `runner.run_parallel` and then reads `jobs/<strategy>/<source>/`, `error.json`, `run.json`, and `designer/`. Cells may be strings or numbers. Null is an em dash, and infinity is ∞.
- It does not run the strategy inside the window process. A worker process is the operator parent. `run_pack` and `walk_forward` still do their own isolation underneath that. Cancel kills the worker. On Linux the worker calls `setsid` and cancel signals the process group, so a walk-forward child does not keep running. On Windows cancel uses `taskkill /T`.

`run_config.json` is written in the report directory before the engine starts. It records the API name, the strategy path and sha256, and every argument, including the nulls. That file is not a metric.

## Report contract

Contract id: `strategy-builder.report.v1`, in `backtester.gui.schema`.

A finished book directory:

| File | Required now | Role |
| --- | --- | --- |
| `metrics.json` | yes | Scalar metrics, nested `benchmarks`, `cost_basis`, `run` |
| `equity_curve.csv` | expected | Session curve the chart reads. Column `equity`. Exposure from `exposure` or `position`. The operator session label comes from `date`, `session`, or `day`, in that order. A designer chart uses `day` or `session`, then the row index. |
| `equity.csv` | written beside the curve | Byte copy of `equity_curve.csv`. Not a substitute when the curve file is missing. |
| `fills.csv` | expected | Fills. Recognized by `shares_delta`, `fill_price`, `fees`, `price`, or `shares_after`. |
| `trades.csv` | optional | Round trips when it is not the fills file. |
| `benchmarks/buy_and_hold_1x/` | expected | Same files for the 1× book. |
| `benchmarks/long_2x/` | Desk | Same files for the constant 2× book. |
| `equity.png` | optional | Saved figure. Shown as an image. The interactive chart uses `equity_curve.csv`. |
| `liquidation.json` | optional | Desk post-mortem. If absent, `metrics.json`'s `liquidation` object is shown. |
| `leaderboard.csv` | optional | Batch board. Fractions as percents, ratios to two decimals, currency, null as —, infinity as ∞. |
| `run_config.json` | written by the window | Launch record. Not required to display an older run. |
| `walkforward.json` | walk-forward | Summary. Legs are `design/` and `locked/`, each a book. The window does not blend them. |
| `batch_status.json` | older batch | Per-strategy ok / failed / cancelled. Not a metric. `error.txt` in a child folder is the failure text. |
| `jobs/<strategy>/<source>/` | Phase 3 batch | One child book, or `error.json` when that child failed. |
| `run.json` | Phase 3 batch | `kind: parallel` and the job list. The designer view does not print tickers from this file. |
| `designer/` | anonymized batch or book | The tree a designer may see. The window reads this instead of the operator files. |

`metrics.json` fields the cards know about, with units:

- Fractions, displayed × 100: `total_return`, `cagr`, `ann_vol`, `max_drawdown`, `hit_rate`, `percent_up_days`, `best_day`, `worst_day`, `financing_rate_annual`, `borrow_rate_annual`, `cash_rate_annual`, `withholding_rate`.
- Dollars: `initial_equity`, `final_equity`, `fees_total`, `slippage_cost`, `borrow_cost`, `costs_total`, `financing_cost`, `dividends_net`, `cash_interest`. A negative amount is `-$340.25`.
- Ratios on a card, three decimals: `sharpe`, `sortino`, `calmar`, `turnover_per_year`, exposure fields, `worst_equity_over_hwm`, `share_factor`, `fee_mult`. The leaderboard prints ratios to two decimals.
- Infinity is ∞ and negative infinity is −∞. Null and NaN are an em dash. The leaderboard sorts infinity as a number and NaN with the blanks.
- Counts: `n_fills`, `round_trips`, `sessions`, `max_dd_duration_sessions`, margin-call counts.
- Leverage, with a ×: `max_gross_leverage`, `avg_gross_leverage`.
- Flag: `disqualified`. `false` is shown as "no". Missing is a dash, not "no". `dividends_applied` is shown as "yes" or "no".
- Accounting, in its own section: `price_basis`, `total_return_method`, `series_kind`, `cash_rate_annual`, `financing_rate_annual`, `withholding_rate`, `accrual`, `sessions_per_year`, `dividends_applied`, `cash_interest`, `financing_cost`, `dividends_net`. The window prints the stored values. It does not recompute interest or a dividend. A redacted designer view keeps the settings (`price_basis`, `total_return_method`, `series_kind`, the three rates, `accrual`, `sessions_per_year`, `dividends_applied`) and drops the dollar amounts. `cash_interest` and `financing_cost` are `rate / 252` times idle or borrowed notional, and `dividends_net` is shares times the per-share amount, so those three reconstruct a price or a share count. A per-share dividend field is not on the allow-list either.

Annualization, stated on those tooltips: 252 sessions per year, risk-free rate 0, session-close marks, first return against initial equity. That is the engine definition. The window does not apply it again.

Any other scalar in `metrics.json` is listed under "Other reported fields" so a Phase 3 addition is visible. Nested objects the window already understands (`benchmarks`, `fees_breakdown`, `period_returns`, `cost_basis`, `run`, `liquidation`) are not repeated there.

If `benchmarks/<name>/metrics.json` and the parent `benchmarks` object disagree on a shared number, the page warns and shows the file.

The drawdown line on the chart is equity / running peak − 1, stored as a fraction. The axis prints that fraction as a percent. The caption under each plot is one sentence. The official max drawdown is the card, which is `max_drawdown` from `metrics.json`. Log scale applies to the equity plot only, and only when every equity point is above zero. In log mode the view is the log10 of those points, with padding in log space, and the axis labels stay in dollars. A tight range still gets several dollar ticks, and a session date is labeled only on an integer session. The crosshair reads equity as money and drawdown as a percent.

Benchmark curves are aligned on the session column. Rows that do not match are not drawn. The count is on the caption tooltip. There is no interpolation.

The fee table lists components only. Each component keeps enough decimal places that the rows add back to the stored total. The footer is that stored total at cents, and it is cleared when the report has no fee breakdown. A designer view does not show that dollar total or the components. It shows the cost drag of the designer book, `fees_total / initial_equity * 10000`, rounded half-up to 0.1 bp, next to the label `designer cost model: 0.8 bp flat`. That drag uses the flat model's fees and the canonical $1,000,000 index (`docs/COSTS_DESIGNER.md`). The same reported figure replaces each benchmark's dollar fee total. Other operator dollar figures stay at two decimals. The operator report keeps the real-fee numbers and can show `cost_comparison.json` beside them. That comparison file stays with the operator report.

The Run page hides fields the selected mode and profile do not use. A blank rate is labeled "profile default". The longer contract text is on the tooltip. Walk-forward uses named files: choosing it clears Pack. The form stays as tall as its fields. The worker log takes the space underneath.

A designer view scrubs calendar dates in several spellings, including `2024/01/04`, `2024-1-4`, `01.04.2024`, `1.4.2024`, `04-Jan-2024`, `04.Jan.2024`, `Jan. 4, 2024`, `Jan.4, 2024`, `January.4, 2024`, `January 4th, 2024`, and `January 2024`. A bare year goes too, but not a number that merely contains one (`2024.50`, `0.2024`). A bare year is a date, including `day 2024`. `2024.50` and `0.2024` stay numbers. Compact eight-digit dates (`20240104`) and two-digit-year month dates (`4-Jan-24`, `01-04-24`) count too. A token that glues a month to a year (`Jan2024`), a year to letters (`FY2024`, `year2024`), or the invoice scale to letters (`scale7.777`, `x7.777`) is dropped on screen and rejected by the export check. Those two checks are the same token decision. Monthly and yearly period keys are shown as `M1`, `M2` and `Y1`, `Y2`, and every stored value stays. The scale words, including `share_factor`, are removed. A ticker is shown as `RANDOM`, including a digit glued on (`QQQ1` becomes `RANDOM1`). A symbol that contains a letter and is longer than one character uses a letter boundary, so the digit does not glue. A one-character symbol, and a symbol that is only digits, uses an alphanumeric boundary, so `V` does not match `v2`, `2` does not match `S00002`, and `10` does not match `S00010`. The publisher uses that same match. The scale check pulls every numeric literal out of a token with one pattern (optional sign, digits, optional decimal, optional exponent), whether or not it is glued to letters. A literal matches when its value equals the scale, the scale's reciprocal, or either value times a power of ten. A match that is only the significand digits, with no agreement in magnitude, needs at least three significant digits. A bare integer matches only when it is the whole token, so a scale of `2` does not eat `long_2x` or `2.5`, and a standalone `2` still does. One-letter tickers count. `MA` inside `max_drawdown` does not. Ticker matching uses casefold, so `ß` matches `SS` and `ﬁ` matches `FI`. Benchmark rows use the same names as the chart (`1× buy and hold`). The designer axis and the crosshair use the `day` or `session` column, and the row index when that column is absent. Column headers and object keys go through the same stripper as values. A failure is the exception summary. Windows, UNC, POSIX, and drive-relative paths (`C:\Users\...` and `C:Users\...`) are removed once they start. The rest of that field is consumed, commas included, and the match stops at a quote or the end of the line. A relative backslash path is removed when it contains `hidden`, `secret`, `graded`, or `NOTES.md`. The word next to a `\\?\` or `//?/` path is removed with it. The status bar and the data-root chip use that same check. Free text keeps a signed number whole (`-1.5e-3`, `-$340.25`, `1,250,000.50`, `-12.5%`) and a hyphenated word (`pre-market`). The regexes still run as a second layer. The operator view still has the traceback and the original ticker. Every designer view, on screen and in the export, keeps one allow-list: returns, ratios, exposure, drawdowns, counts, bar or session indices, accounting settings, exact equity, and the cost drag in basis points. The published `designer/` tree is that same list. Share counts, prices, notionals, per-component fees, the dollar fee total, and accounting dollars are dropped. A field that is not on the list is hidden, including one added later. Period labels are the engine blocks `S01`–`S60` and `B01`–`B60` and the month and year stand-ins `M1`–`M600` and `Y1`–`Y60`. `S99` is not one of those labels. The run directory is shown as `<designer run>` when the path itself was removed, and the status bar does not keep an operator sentence on that screen. Numeric values of allow-listed keys are not scale-scanned. Free text, identifiers, filenames, labels, and string values are. That scan NFKC-normalizes the text and maps a unicode minus to `-` before it reads a literal, so `build2_1e−7` and fullwidth `７．７７７` are the same leak as the ASCII spelling. A clock (`09:25` or `09:25:00`) is one token: the colon stays, and the token is not the scale. A match that is only the significand digits, with no agreement in magnitude, needs at least three significant digits. The exact value, the reciprocal, and a power of ten still match at one or two digits, so a standalone `25` is still the scale `0.25`.

The page title follows the audience (`Operator report` or `Designer report`). While a designer report is open, the status bar and the data-root chip, including the chip tooltip, go through the same designer text filter, so an earlier `Inspected QQQ` becomes `Inspected RANDOM` and a POSIX path does not stay on screen. A failed load clears the previous book. That includes a file that cannot be decoded, a truncated `metrics.json` in a walk-forward leg, a `MemoryError`, and a report file over the read cap (16 MB JSON, 256 MB CSV, 32 MB PNG). The invoice line, the chart and its caption, the readout, and the activity, benchmark, fee, period, and detail tables all go. The banner says the report could not be read. A saved figure that does not decode says `figure could not be read`. The Run page shows an anonymization rejection when Phase 3 refuses a symbol, instead of only the last wrapper line.

Load keeps the real directory. The field may show a scrubbed path. Switching audience reloads the real directory, and Load follows the field when it was edited. A designer export writes the columns on screen, for a redacted operator folder and for a published `designer/` tree. It does not copy `equity_curve.csv`, `equity.csv`, or `fills.csv`, and it drops close, price, and the share-count columns. `metrics.json`, `displayed_metrics.csv`, and `leaderboard_view.csv` go through the same allow-list, and a file that fails is withheld. Per-share fee schedules are public, and the equity curve is exact, so a determined designer can still infer a share count from the path of equity. That residual is accepted and described in `docs/PACK.md`. The window does not hide equity, and it does not change the engines or the fee schedule. It withholds the dollar fee total so the inference is not a single division. `share_factor` fails that check the same way `share_scale` does. A designer PNG is rewritten before it is shown or exported. PNG text is still checked after decompression, including a compressed `iTXt` chunk read as UTF-8. A percent that would print as eight zeros is shown in scientific form. The path check and the scale check are the same functions the screen uses. `\\?\` and `\\?\UNC\` count as paths, and once a path starts the rest of that field is consumed, commas included. A drive-relative path (`C:Users\...`, `C:secret\NOTES.md`) and a relative backslash path containing `hidden`, `secret`, `graded`, or `NOTES.md` count too. A token is dropped when any literal inside it matches the invoice scale by value, reciprocal, or a power of ten (`build2_7.777`, `build2_7777e-3`, `7.777e-3`, `7.777e3`, `build2_1e-7`, `v2_1e+7`). A significand-only match needs at least three significant digits. The screen keeps `long_2x` and `2.5` when the scale is 2. A designer PNG is rewritten to the chunks a renderer needs (`IHDR`, `PLTE` when present, `IDAT`, `IEND`, plus `tRNS`, `gAMA`, `sRGB`, and `pHYs` when present). Text chunks and bytes after `IEND` are removed, and a file that is not a PNG does not display. The text-chunk check remains as a second gate: `iTXt` is decoded as UTF-8, the text is NFKC-normalized, and bytes after `IEND` are included. Pixel bytes are not text. A free-text decimal of at least 1, such as `2.5` or `450.12`, fails that export check even when it is not the scale. The screen still shows the token. An allow-listed numeric cell, including a formatted equity value, is exempt. A formatted number under an allow-listed column key gets that same exemption. The column is identified by its key, not by a display label such as `Sharpe`. A designer table writes that key in the header and adds the label only when the label itself passes the check. A designer-visible strategy id is decided on the original id, before any scrub. After NFKC it must match a leading letter and then letters, digits, underscore, or hyphen, at most 64 characters. It cannot contain a run of four digits, a month token, a calendar token (`q1`–`q4` next to other digits, `fy` / `h1` / `h2` / `wk` / `week` followed by digits, `today`, `holiday`, `yesterday`, `tomorrow`, `eod`, or a weekday), or three numeric tokens of one or two digits. `vol_1.5` is not an id. `long_2x`, `ma_200`, `ma_20_50`, `rsi14`, and `v2` are. The price rule does not apply to an id that passes that grammar. The scale and the ticker still do, and the path check and the audit call the same grammar. A withheld file is named in the status bar with the reason. `publish_designer` keeps a string on an allow-listed key only when that key allows it: an enum, a clock, a session label, or a safe id. `message` and `error` keep a fixed code or are dropped. That publish then scans the tree with the operator scale. A scale or a price in the remaining text deletes `designer/` and the publish fails. The chart in that tree is the canonical PNG. A `pack_` source id whose tail is hex is stored as an opaque letter id; the operator copy keeps the original id. An unsigned integer exponent matches when its value is the scale, including inside a raw `pack_` id (`pack_2e2abcdef0` at scale 2, `note25e2` at 0.25). `export_allowed` and the publication scan read the text as stored. A source id on disk is the letter form. A raw id that is still in the text is not rewritten before either gate, so a scale inside it fails both. One identifier check covers a signed exponent and a leading digit: when that token is not the scale, the export gates allow it. A leading digit is not a designer directory. `QQQ1` is published as `RANDOM1`. `1QQQ` fails that one job. A non-finite or overflowing logarithm is not a match.

## Screenshots

Rendered offscreen at 1680×1180. These are fixture reports and the real profile text, not a backtest of the sample month.

- `docs/screenshots/01-data-dark.png`
- `docs/screenshots/02-packs-dark.png`
- `docs/screenshots/03-strategies-dark.png`
- `docs/screenshots/04-run-dark.png`
- `docs/screenshots/05-results-dark.png`
- `docs/screenshots/06-desk-risk-dark.png`
- `docs/screenshots/07-history-dark.png`
- `docs/screenshots/08-results-light.png`
- `docs/screenshots/09-run-light.png`
- `docs/screenshots/10-desk-risk-light.png`
- `docs/screenshots/11-leaderboard-dark.png` — a real Phase 3 desk batch
- `docs/screenshots/12-leaderboard-light.png`
- `docs/screenshots/13-results-log-dark.png`
- `docs/screenshots/14-results-log-light.png`
- `docs/screenshots/15-run-walk-dark.png`
- `docs/screenshots/16-run-walk-light.png`
- `docs/screenshots/17-run-pack-dark.png`
- `docs/screenshots/18-run-pack-light.png`
- `docs/screenshots/19-fees-dark.png`
- `docs/screenshots/20-fees-light.png`
- `docs/screenshots/21-designer-dark.png` — scrubbed designer view, period labels `M1` / `Y1`
- `docs/screenshots/22-designer-light.png`

## Tests

`tests/gui/` holds the adapter tests, the validation tests, and the pytest-qt tests. The Qt tests skip when PySide6 is not installed, so `python -m backtester test` on a core install still passes. `.github/workflows/gui.yml` installs the extra and runs `tests/gui` offscreen on Ubuntu and Windows, Python 3.11–3.13.

## Known gaps

- The engine does not report bar progress. The window shows the stage and, for a batch, which strategy is running. It does not invent a percent complete for one book.
- Cancel leaves whatever files the process had already written. The page says a partial folder is not a finished report. `metrics.json` is only trusted after the worker status is `done`.
- A batch calls `backtester.runner.run_parallel` with a progress callback and a cancel token. The window does not implement the scoring. Until Phase 3 replaces the stub, that call raises and the page shows the message. The loader already reads the Phase 3 tree.
- Walk-forward from the window is named files only. `walk_forward` takes a frame. A pack is a single `run_pack`.
- The equity PNG is displayed when present. The interactive chart is always the CSV.
- There is no referee page and no multi-name book. Those are later phases.
- OS-level isolation is still Phase 4. The window does not weaken the Phase 1 rules, and it does not claim to be the jail.
- A strategy is imported in the worker, which is the same process the CLI uses for `run`. It is not imported in the window process.
- Linux and the CI image do not have Segoe UI or Cascadia Mono. The type scale is the same; the family falls through the list above.
