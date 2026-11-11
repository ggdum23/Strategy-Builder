# Profile — compression regime, long or flat

**Lens.** Volatility squeeze, revised by the design-window evidence. Directional breakouts (buy a new high, short a new low) did not beat buy-and-hold after costs. Compressed range did forecast better risk-adjusted drift, and expanded range is where the deep drawdowns sat. The shipped rule trades that regime, not the breakout direction.

**Thesis.** ASSET pays a positive drift. That drift is a better trade when recent true range is quiet, so the book holds 1.5×. When range is loud relative to the last year, gap losses cluster and the book goes flat until the range cools. It fails when a quiet market gaps before the trailing range can reclassify, and when a long advance happens entirely inside an expanded-range regime.

**Decision frequency.** Once per regular session, on the last bar (minute 959, or 779 on a shortened session). The new target fills at the next session’s open. Extended hours are not used.

**Rules.** ATR(20) / close, percentile rank versus the last 252 values including today.

| Regime | Enter | Exit back to neutral | Exposure |
|---|---|---|---|
| Compressed | rank ≤ 0.25 | rank ≥ 0.40 | +1.5 |
| Neutral | otherwise | — | +1.0 |
| Expanded | rank ≥ 0.70 | rank ≤ 0.55 | 0 |

No shorts. No order if the target is unchanged. Flat until 271 sessions of history exist (the percentile’s warmup).

**Indicators.** Session high, low, close; true range versus the prior close; 20-session mean true range; 252-session percentile rank of ATR/close.

**Sizing.** Target is exactly 0, 1.0, or 1.5. Shares = trunc(target × equity at the next open / open). Cap is 1.5, under the engine limit of 2. Average absolute exposure on the full window is 0.74. About 41% of sessions end flat, 27% near 1×, 32% near 1.5×.

**Risk controls.** Long only. Hard cap 1.5×. Expanded-range state is the de-risk rule (target 0). Hysteresis limits churn. Turnover is about 6.7× equity per year, so 2× slippage barely moves Sharpe. A human might cut risk after a single session worse than about −7% while the book is at 1.5×, or stop the book if drawdown exceeds −35% or if two consecutive 252-session periods both have negative Sharpe. None of that is in the code.

**Variants.** 1,203 unique parameterizations, 1,210 backtests, all on days 0–2999. Validation (days 3000–3795) was run once after the file was frozen.

| | Full | Design | Validation | BH full | BH design | BH validation |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 420.7% | 222.6% | 61.4% | 434.2% | 193.9% | 81.7% |
| CAGR | 11.6% | 10.3% | 16.4% | 11.8% | 9.5% | 20.8% |
| Vol | 14.1% | 14.6% | 12.1% | 20.0% | 20.7% | 17.0% |
| Sharpe | 0.846 | 0.745 | 1.318 | 0.656 | 0.541 | 1.198 |
| Sortino | 1.191 | 1.051 | 1.846 | 0.932 | 0.770 | 1.680 |
| Max drawdown | −22.5% | −22.5% | −15.8% | −53.5% | −53.5% | −23.1% |
| DD duration (sessions) | 579 | 534 | 160 | 798 | 798 | 157 |
| Calmar | 0.516 | 0.460 | 1.036 | 0.220 | 0.177 | 0.900 |
| Turnover / year | 6.69 | 6.29 | 7.36 | 0.03 | 0.05 | 0 |
| Round trips | 28 | 20 | 8 | — | — | — |
| Hit rate | 53.6% | 50% | 62.5% | — | — | — |
| Avg hold (sessions) | 77 | 79 | 52 | — | — | — |
| Fees | $34,590 | $26,302 | $2,569 | $294 | $294 | $0 |
| Slippage | $24,845 | $14,683 | $3,150 | $100 | $100 | $0 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |
| Avg \|exposure\| | 0.74 | 0.73 | 0.77 | 1.00 | 1.00 | 1.00 |
| Long / flat time | 59% / 41% | 58% / 42% | 62% / 38% | 100% / 0% | 100% / 0% | 100% / 0% |
