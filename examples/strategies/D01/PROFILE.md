# Profile — ASSET volatility-targeted long

**Lens.** Started from time-series momentum. Past returns do not positively predict future returns on the design window (variance ratios below 1 from 2 to 252 sessions). The rule that was kept is inverse-volatility sizing of a long position: volatility clusters (21-session vol forecasts the next 21 with R² ≈ 0.48) and the asset’s drift does not scale with that forecast.

**Thesis.** Stay long. Hold more when recent realized volatility is quiet, less when it is elevated, and never short. The edge fails on the first leg of a selloff that starts from a calm tape (the book is at the 1.5 cap) and on the high-vol rebound that follows (the book is small). Sessions 2520–2999, held out of the selection score, were a mild case: Sharpe 0.26 vs buy-and-hold 0.40.

**Decision frequency.** Once per regular session, on the last RTH bar (15:59, minute 959; 12:59, minute 779, on a shortened session). Fill at the next session’s open. Extended hours are not traded.

**Sessions.** Every regular session, including shortened ones. Flat only until 63 close-to-close returns exist.

**Rules.** Let RV be the average of the 21- and 63-session annualized sample volatilities of close-to-close returns. Target = round-half-up(min(1.50, 0.22 / max(RV, 0.05)), step 0.25). Higher target buys the difference; lower target sells the difference; the book stays long. An unchanged bucket does not trade.

**Indicators.** Close-to-close returns, RV_21, RV_63, equal-weight blend. No moving average, channel, weekday, or volume input in the frozen rule.

**Sizing.** The target is the equity fraction. Engine share count is trunc(target × equity at the fill open / open). Cap 1.50. Average close exposure 1.22 on the full history; maximum marked exposure 1.59. Turnover about 3× per year.

**Risk controls.** Cap, quarter-turn grid, long-only, warm-up flat. No intraday stop and no equity-curve kill in the code. Stop in practice if the strategy’s own drawdown exceeds 45%, if a two-year Sharpe lags buy-and-hold by ~0.4, or if debit interest on ~0.3 of equity costs more than ~4% a year (the engine charges none).

**Variants.** 123 strategy variants plus buy-and-hold. Round 1: 106 directional, peak, reversal, and single-lookback vol rules. Round 2: 15 precommitted blend-vol and crash-filter specs; the unfiltered 0.22 blend won on sessions 252–2519. Two daily session-splits (overnight-only, intraday-only) were rejected on costs. Validation (sessions 3000–3795) was run once after the freeze.

| | Strat full | B&H full | Strat design | B&H design | Strat valid. | B&H valid. |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 845.6% | 434.2% | 371.5% | 193.9% | 100.5% | 81.7% |
| CAGR | 16.1% | 11.8% | 13.9% | 9.5% | 24.6% | 20.8% |
| Ann. vol | 20.6% | 20.0% | 20.9% | 20.7% | 19.5% | 17.0% |
| Sharpe | 0.827 | 0.656 | 0.728 | 0.541 | 1.229 | 1.198 |
| Sortino | 1.16 | 0.93 | 1.02 | 0.77 | 1.69 | 1.68 |
| Max drawdown | −39.4% | −53.5% | −39.4% | −53.5% | −22.8% | −23.1% |
| DD duration (sessions) | 592 | 798 | 592 | 798 | 224 | 157 |
| Calmar | 0.41 | 0.22 | 0.35 | 0.18 | 1.08 | 0.90 |
| Turnover / year | 3.00 | 0.03 | 3.42 | 0.05 | 2.50 | 0 |
| Avg abs exposure | 1.22 | 1.00 | 1.21 | 1.00 | 1.27 | 1.00 |
| Final equity | $9.46M | $5.34M | $4.72M | $2.94M | $2.01M | $1.82M |

Official numbers from `pack/run_backtest.py` on the unmodified engine. Round trips are 0 for both books: one open long, resized rather than flattened.
