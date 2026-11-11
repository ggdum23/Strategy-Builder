# Profile — multi-horizon trend, long or flat

**Lens.** Opening gaps, weekday effects and shortened sessions. Those gross patterns are real on the design window. A round trip costs more than the weekday or gap anomaly, and shortened sessions are too rare to fit. The strategy that survived costs is slower: hold through gaps and weekdays, and only change exposure when the multi-month trend or the volatility scale changes.

**Thesis.** Close-to-close returns continue at the 3-, 6- and 12-month horizons often enough to skip part of the deep buy-and-hold drawdowns. Recent volatility clusters, so a 15% vol scale cuts size after turbulent stretches. The book stays long or flat because the unconditional drift is positive; shorts were worse on the train window.

**Decision frequency.** Once per regular session, on the last bar: minute 959 (15:59), or minute 779 (12:59) when `short_session` is 1. The fill is the next regular-session open (minute 570). Extended hours are off.

**Sessions.** Every regular session, including shortened ones. Same rule every weekday.

**Rules.** Flat until 253 closes are stored. Then count how many of the 63-, 126- and 252-session changes are positive. Fewer than two: target 0. Two or three: target = min(1, 0.15 / σ₆₀), rounded half-up to 0.25 steps, and sent only if it differs from the current target by at least 0.25. σ₆₀ is the sample standard deviation of the last 60 close-to-close returns, annualized by √252.

**Indicators.** Three sign-of-momentum flags and one 60-session realized-volatility estimate. No weekday, gap or short-session indicator in the live rule.

**Sizing.** Gross exposure is 0, 0.25, 0.50, 0.75 or 1.0. No short. No use of the 2x allowance. Average absolute exposure is about 0.69 on the full sample; about 24% of sessions close flat and about 55% close near 1x.

**Risk controls.** The flat trend gate and the volatility cap are the de-risk rules. There is no intraday stop: an overnight gap is taken at the current size. A discretionary stop, not in the code, would be a 30% peak-to-trough equity drawdown (observed maximum was 16%).

**Variants.** 768 design-window evaluations (506-parameter walk-forward grid plus later families). The validation window was run once after the rule was frozen.

| | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 291.8% | 434.2% | 152.1% | 193.9% | 55.4% | 81.7% |
| CAGR | 9.49% | 11.77% | 8.08% | 9.48% | 14.99% | 20.82% |
| Ann. vol | 12.2% | 20.0% | 12.0% | 20.7% | 13.3% | 17.0% |
| Sharpe | 0.80 | 0.66 | 0.71 | 0.54 | 1.12 | 1.20 |
| Max drawdown | -16.0% | -53.5% | -16.0% | -53.5% | -14.1% | -23.1% |
| Calmar | 0.59 | 0.22 | 0.51 | 0.18 | 1.06 | 0.90 |
| Turnover / year | 4.56 | 0.03 | 5.09 | 0.05 | 3.62 | 0.00 |
