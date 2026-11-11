# Profile

**Lens.** Volatility targeting with leverage. Exposure scales with the inverse of forecast volatility, up to 2× when volatility is low. No shorts.

**Thesis.** ASSET pays a positive drift and its volatility clusters. Keeping a roughly constant risk budget (18% annualized) cuts exposure after volatility rises and uses the engine’s zero financing rate to lever when volatility is low. The edge is the shape of the path, not a directional signal. It fails when the first crash day gaps through before the forecast updates, when a rebound starts while the forecast is still high, and in a calm bull market where the extra leverage is not paid for.

**Decision frequency.** Once per regular session, on the last RTH bar: minute 959 (15:59), or minute 779 (12:59) on a short session. The order fills at the next session’s open. Pre/post bars are not used.

**Rules.** EWMA variance of daily log close-to-close returns, λ = 0.90 (half-life 6.6 sessions), prior volatility 20% until the first return. Weight = clip(0.18 / σ, 0, 2), rounded to the nearest 0.25. Publish the new weight only if it differs from the live target by at least 0.50. Otherwise keep the target. The book is long from the first close onward; it was flat only before that first fill.

**Indicators.** One: the annualized EWMA volatility above. No price filter, no weekday rule, no intraday signal.

**Sizing.** Target fraction of equity, whole shares via the engine (`trunc(target × equity_at_open / open)`). No drift rebalance while the target is unchanged. Median target on the full sample is 1.25; about 1% of sessions are at the 2× cap; the lowest target reached is 0.25. Realized volatility is 18.6% against the 18% target.

**Risk controls.** Hard cap at 2×. The variance update is the de-risk rule. The half-unit band is the cost control (about 9 target changes a year, turnover ≈ 4.8×). No equity stop in the code: the design drawdown lasted 597 sessions and then recovered. A human would stand down if live slippage stayed far above the 10 bp stress, if trailing realized volatility left the 8–30% band, or if the session clock the decision minute assumes no longer matched the feed. None of that is in the code.

**Variants.** 3,184 evaluated. 24 costless λ × target-vol paths on days 0–2999, then 3,160 costed variants on days 0–1999 (constant leverage, single and dual EWMA, inverse-variance, contrarian tilt, time-series momentum, one-day reversal, one weekday rule). Parameters frozen before the day 2000–2999 holdout and before validation. Holdout lagged buy-and-hold; validation did not. No retune.

| | Full strategy | Full B&H | Design strategy | Design B&H | Validation strategy | Validation B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 532.7% | 434.2% | 212.1% | 193.9% | 102.7% | 81.7% |
| CAGR | 13.03% | 11.77% | 10.03% | 9.48% | 25.07% | 20.82% |
| Ann. vol | 18.55% | 20.00% | 18.53% | 20.73% | 18.65% | 17.00% |
| Sharpe | 0.753 | 0.656 | 0.609 | 0.541 | 1.294 | 1.198 |
| Sortino | 1.041 | 0.932 | 0.841 | 0.770 | 1.789 | 1.680 |
| Max drawdown | −29.86% | −53.50% | −29.86% | −53.50% | −22.76% | −23.14% |
| DD duration (sessions) | 597 | 798 | 597 | 798 | 160 | 157 |
| Calmar | 0.436 | 0.220 | 0.336 | 0.177 | 1.102 | 0.900 |
| Round trips | 0 | 0 | 0 | 0 | 0 | 0 |
| Hit rate | n/a | n/a | n/a | n/a | n/a | n/a |
| Turnover / year | 4.84 | 0.03 | 4.82 | 0.05 | 4.86 | 0.00 |
| Fees | $24,373 | $294 | $18,361 | $294 | $1,927 | $0 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |
| Avg \|exposure\| | 1.157 | 1.000 | 1.121 | 1.000 | 1.294 | 1.000 |
