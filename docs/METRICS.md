# Metric definitions

Both profiles score a book from session marks. The formulas below are the ones
the engines write into `metrics.json`. Arena's `metrics` function is the
vendored reference function. A byte-identical comparison against
`tests/reference/arena/backtest.py` is part of the suite. `percent_up_days` is
the one shared field the reference function does not emit. The report layer
adds it from the same daily equity series. It is not inserted into
`engine_arena.metrics`, so the reference comparison stays exact.

Desk uses the same formulas on its own marks. The two profiles are not merged.
Arena counts delivered bars (regular session, plus pre/post only when the
strategy asks for extended hours). Desk counts every bar, including pre and
post. A holding period in bars is therefore not comparable across profiles.

## Marks and the year

A year is 252 sessions. It is not 365 calendar days.

`sessions` is the number of daily marks, `n`. The `years` field is
`round(n / 252, 3)`. CAGR, volatility, Sharpe, Sortino, and turnover do not
use that rounded field. They use `n / 252` unrounded.

Arena's daily mark is the last regular-session close of the session, after
borrow has been charged when the book is short. Desk's daily mark is the last
regular-session close. Desk financing is not charged on that mark. It is
charged at the next session boundary, on a snap of the previous session's last
bar of any phase.

The first daily return is `equity[0] / initial_equity - 1`. Every later return
is `equity[i] / equity[i - 1] - 1`. A flat day is a return of 0.

A denominator of 0 has no defined return. That session's return is stored as
0. It is not an up day. It does not raise, and it is not stored as infinity.
`total_return` is 0 when initial equity is 0. `cagr` is -1 in that case: the
ratio is undefined and is not reported as an infinite gain. A drawdown whose
running peak is 0 is 0. A block whose starting equity is 0 has period return
0. A path with no zero mark uses the same divisions as before, and Arena's
byte comparison with the vendored reference still holds on those paths.

## Return, risk, and the path

`total_return` is `final_equity / initial_equity - 1`.

`cagr` is `(final / initial) ** (1 / (n / 252)) - 1` when final equity is
positive. When final equity is not positive, Arena reports `-1`. Desk uses the
same rule and saturates an overflowing power at positive or negative infinity.
The rounded `years` field is not the exponent.

`ann_vol` is the sample standard deviation of the daily returns
(`ddof = 1`) times `sqrt(252)`. One session, or a zero sample deviation, has
volatility 0. It is not `sqrt(365)`.

`sharpe` is `mean(returns) / sample_stdev * sqrt(252)` with a risk-free rate
of 0. One session, or a zero sample deviation, has Sharpe 0. It is not
`sqrt(365)`.

`sortino` uses a downside deviation of `sqrt(mean(min(return, 0) ** 2))`
over all `n` sessions. Zero-return days stay in the mean. They are not
dropped. If that downside deviation is 0, Sortino is 0, not infinity. Otherwise
Sortino is `mean(returns) / downside * sqrt(252)`.

`max_drawdown` walks the path `[initial_equity, *daily equity]`. The peak is
the running maximum. Drawdown at each point is `value / peak - 1`. The max
drawdown is the minimum of that series. A book that never closes below its
peak has max drawdown 0.

`max_dd_duration_sessions` is the longest streak of strictly negative
drawdowns on that path, excluding the initial point. The unit is sessions
(daily marks), not bars and not calendar days. A day that ties the peak has
drawdown 0 and resets the streak. A drawdown that has not recovered by the
last mark counts through that last mark.

`calmar` is `cagr / abs(max_drawdown)` when max drawdown is negative. With no
drawdown it is positive infinity. `metrics.json` writes non-finite numbers as
JSON null. `leaderboard.csv` writes `inf` and `-inf`. `load_report` on a
parallel run returns contract `strategy-builder.report.v1` and types those
cells: a blank is null, `true` and `false` are booleans, `inf`, `-inf`, and
`nan` are floats, and a whole number is an int. `read_leaderboard` still
returns the CSV text, including the string `inf`.

`best_day` and `worst_day` are the maximum and minimum daily returns.

`percent_up_days` is the fraction of sessions whose return is strictly
greater than 0. A flat session is not an up day. The first session is
compared with initial equity. A session whose previous mark is 0 is not an
up day and stays in the denominator. No marks produces null. The function
does not divide by zero.

## Trades

A round trip is a closed episode. An episode opens when the share count goes
from 0 to non-zero, or when a fill flips the sign. The old side closes and
the new side opens. Fees on a closing fill are split by
`abs(old shares) / abs(delta)`. A partial scale-up or scale-down does not
close the episode. A position that is still open at the last bar is
`open_position_at_end`. It is not a round trip and it is not in the hit rate.

`round_trips` is the number of closed episodes. `hit_rate` is the fraction of
those episodes with `pnl > 0`. A pnl of 0 is not a hit. No closed episodes
makes the hit rate null. `long_round_trips` and `short_round_trips` count
closed episodes by the sign of the side.

`avg_holding_sessions` is the mean of `end - start` over closed episodes.
Arena uses the day index. Desk uses the session ordinal. The difference is
not inclusive: a round trip that opens and closes on the same session has
holding 0. `avg_holding_bars` is a different number. Arena counts delivered
bars, including the fill bar, and does not count a bar the strategy did not
receive. Desk counts every bar, including pre and post. Warm-up bars before
the scored window are not in the Arena statistic, because the warm-up is
rescaled away. Desk warm-up is flat and is not a live episode.

`turnover_per_year` is `traded_notional / mean(daily equity) / (n / 252)`.
`traded_notional` is the sum of `abs(delta) * unslipped reference price`.
The reference price is the bar open for a normal fill and the close for a
Desk liquidation. The divisor is the unrounded `n / 252`, not the rounded
`years` field. If mean daily equity is not positive, turnover is null.

`n_fills` counts fills, including a liquidation fill. An unchanged target
does not rebalance and does not add a fill.

`fees_total` is the engine sum of commission, exchange, clearing,
pass-through, CAT, SEC, and TAF. `costs_total` is fees plus slippage plus
borrow. Desk also adds financing. Dividends are not part of `costs_total`.
They are `dividends_net`.

## Exposure and blocks

Close exposure is the `exposure` on the daily mark. It is 0 when equity is
not positive. `avg_net_exposure_close` and `avg_abs_exposure_close` are the
mean and the mean of the absolute value. `frac_sessions_long_at_close` and
`frac_sessions_short_at_close` are the fraction of marks strictly long and
strictly short.

Bar exposure (`avg_abs_exposure_bars`, `frac_bars_long`, `frac_bars_short`)
is computed only on bars where equity is positive. It is not the close series.

`period_returns` chains 252-session blocks. The next block starts at the
previous block's ending equity. The first block starts at initial equity.

Arena labels a mark `S{(day - first_day) // 252 + 1:02d}`. `day` is the day
index of that mark, and `first_day` is the day index of the first mark. It
is not the row number. Desk labels a mark
`B{(ordinal - first_ordinal) // 252 + 1:02d}` from the session ordinal.

## Desk fields

These are present on Desk books and absent on Arena books.

`financing_cost` is the cash charged at session boundaries:
`rate / 252 * max(0, abs(shares * snap_price) - snap_equity)` with rate 0.07
unless the run overrides it. The snap is the previous session's last bar.
`borrow_cost` is `0.0025 / 252` of short notional on that same snap, unless
the run overrides the rate. A long pays no borrow.

`dividends_net` is cash from dividends. The first live session does not pay
one, because there is no previous live close.

`margin_calls_filled` counts margin-call fills. A gross leverage above 3.15
queues a reduction to ±3 on the next regular open.

`max_gross_leverage` and `avg_gross_leverage` summarize gross leverage on
bars where it is defined.

`worst_equity_over_hwm` is the lowest `equity / high_water` seen on any bar.
`disqualified` is true when a bar's equity is strictly below 30% of the
running high-water mark. The engine then flattens at that bar's close and
writes `liquidation` on the result. The report writes `liquidation.json`
only in that case. The object records session, time, phase, close,
equity before, high-water, shares before, equity after, and shares after.

## Benchmarks and the leaderboard

Benchmarks run through the same engine and the same costs as the strategy.
Arena's only benchmark is 1x buy-and-hold. Desk also runs a constant 2x long
financed at 7% (or the run's financing rate, when one was passed). On an
isolated run the parent runs those benchmarks again and recomputes the
strategy statistics from the child's equity and fills. A mismatch fails the
row. The published figures are the parent's. A crash before the benchmarks
leaves them unrun. See `docs/ISOLATION.md`.

Desk is judged on raw total return against the 1x book. The leaderboard key
is `excess_total_return = strategy total_return - buy_and_hold_1x total_return`
for both profiles. Sharpe is not the key. Rows sort by failures last, then
by finite excess descending, then by `strategy_id`, then by `source_id`.
The rank column is assigned after that sort. It does not depend on which
job finished first, or on the worker count. Wall-clock time, process id, and
completion order are not written into `metrics.json` or `leaderboard.csv`.

`bh_total_return` is the 1x book's total return. `long_2x_total_return` is
the Desk 2x book's total return and is empty for Arena.

## What a designer may see

An anonymized run writes two trees. The operator files keep the original ticker and
`cost_basis.share_scale`. The `designer/` tree replaces the ticker with `RANDOM`, drops calendar
dates, and the scale fields (`symbol`, `share_factor`, `share_scale`,
`price_factor`, `volume_factor`, `date_map`, `cost_basis`, the sidecar, and
the source paths). ISO dates are blanked. A parallel batch puts the designer
leaderboard and each anonymized job under one `designer/` directory. Hand
over that directory. Named runs are the operator's own data and are not
rewritten into that tree.

The equity chart is session index against equity. The title is `equity`.
It has no ticker and no date axis. PNG bytes are not part of the determinism
contract. An encoder may stamp the file.

Post-fee cash still determines `share_scale`. That residual is unchanged and
is described in `docs/PACK.md`. Redaction removes the factor fields. It does
not promise that the numeric factor is absent from a scaled price.

## Accounting switches

Every book records the price basis and the rates that produced it. The keys are flat on `metrics.json` and repeated under `accounting`:

| Key | Meaning |
| --- | --- |
| `price_basis` | `raw` or `total_return` |
| `total_return_method` | `none`, `profile_dividends`, `cash_dividends`, or `registry_series` |
| `series_kind` | `raw` or `total_return`, the kind the file was declared as |
| `cash_rate_annual` | Decimal earned by idle cash. 0 is the default |
| `financing_rate_annual` | Decimal charged on borrowed money. Arena default 0, Desk default 0.07 |
| `withholding_rate` | Fraction of a positive long dividend withheld. Default 0 |
| `accrual` | `session_boundary_1_252` |
| `sessions_per_year` | 252 |
| `dividends_applied` | True when a dividend file was credited |

`cash_interest` is the dollars credited. `financing_cost` is the dollars charged. `dividends_net` is the cash dividend after withholding. Cash interest is income, not part of `costs_total`. Financing is.

The same keys are on `run.json`, the leaderboard, and (for `price_basis` only, as the series kind) the pack manifest and sidecar. `write_leaderboard` raises when stamped rows disagree. A row with no `price_basis` is an older board and is not part of that check.

Designer files may contain the closed tokens `raw`, `total_return`, `none`, `profile_dividends`, `cash_dividends`, `registry_series`, and `session_boundary_1_252`. Those cells are vocabulary, not a ticker. The stand-in for a real ticker is still `RANDOM`.
