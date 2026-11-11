# ASSET profile

**Lens.** Contrarian overreaction with a trend filter. The intraday version of that lens (fade the opening drive) failed a design-window screen: opening selloffs continued. The multi-day version survived. A volatility kill-switch was added because a plain trend filter left too much of the large design-window drawdown on.

**Thesis.** Stay long the close-to-close drift. Add to 2× only after a 5% pullback from the 20-session high while the close is still above the 200-session mean and 20-day volatility is under 30%. Cut to 0.5× when the long trend is broken or volatility is at least 30%, and to flat when both are true. Never short. The add is the overreaction. The cut is what keeps a clustered selloff from becoming a 50% drawdown. The rule lags buy-and-hold in a smooth bull market, which is what the validation window was.

**Decision frequency.** Once per regular session, on the last bar (minute 959, or 779 on a shortened session). Fill at the next session’s open. Extended hours are not used.

**Rules (first match wins).** After 200 closes: flat if vol-kill and trend broken; else 0.5 if vol-kill or trend broken; else 2.0 if drawdown from the 20-session high is −5% or worse; else 1.0. Before 200 closes, target 1.0. Unchanged targets send no order.

**Indicators.** 200-session simple mean of regular closes. 20-session high, drawdown \(C/H-1\). 20-session sample standard deviation of close-to-close returns, annualized with \(\sqrt{252}\), kill at 30%.

**Sizing.** Target is the fraction of equity at the next open, in {0, 0.5, 1.0, 2.0}. Whole shares, trunc toward zero. Average absolute exposure about 0.91 on the full history. The 2× state is on about 4% of sessions. No borrow.

**Risk controls.** Discrete cap at 2×. No shorts. Vol kill at 30%. Half exposure below the long average. Engine clamps [−2, 2] and charges 1 bp slippage plus the pack fee schedule.

**Variants.** 802 costed design-window simulations (386 + 202 + 205 + 9). Five intraday opening windows were screened and dropped before a portfolio test. Validation (days 3000–3795) was run once after the file was frozen.

| Metric | Design strat | Design B&H | Valid strat | Valid B&H | Full strat | Full B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 209.7% | 193.9% | 75.4% | 81.7% | 443.2% | 434.2% |
| CAGR | 9.96% | 9.48% | 19.5% | 20.8% | 11.9% | 11.8% |
| Ann. vol | 16.4% | 20.7% | 19.3% | 17.0% | 17.1% | 20.0% |
| Sharpe | 0.661 | 0.541 | 1.017 | 1.198 | 0.744 | 0.656 |
| Sortino | 0.943 | 0.770 | 1.416 | 1.680 | 1.054 | 0.932 |
| Max drawdown | −20.2% | −53.5% | −16.4% | −23.1% | −20.2% | −53.5% |
| DD duration (sessions) | 431 | 798 | 258 | 157 | 431 | 798 |
| Calmar | 0.493 | 0.177 | 1.190 | 0.900 | 0.588 | 0.220 |
| Round trips | 10 | 0 | 5 | 0 | 15 | 0 |
| Hit rate | 50% | — | 40% | — | 47% | — |
| Avg hold (sessions) | 260 | — | 103 | — | 220 | — |
| Turnover / year | 10.7 | 0.05 | 14.6 | 0 | 12.2 | 0.03 |
| Fees | $47.7k | $294 | $5.1k | $0 | $63.2k | $294 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |
| Avg abs. exposure | 0.89 | 1.00 | 0.97 | 1.00 | 0.91 | 1.00 |
