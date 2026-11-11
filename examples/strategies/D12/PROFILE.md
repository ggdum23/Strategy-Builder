# PROFILE — ASSET volatility-managed long

**Lens.** Research started from multi-timeframe trend confirmation (intraday, multi-day, multi-month). That family was tested and lost to a simpler rule on the design train window. The shipped strategy is volatility targeting: hold ASSET long, and scale the position to a 10% annualized volatility target.

**Thesis.** ASSET drifts up, and its volatility clusters. High-vol stretches have similar average returns to quiet stretches and much larger variance, so a full position has a worse conditional Sharpe when recent vol is high. Cutting exposure as realized vol rises keeps the book near a constant risk and avoids being fully invested through the worst clusters. The same backward-looking vol estimate is late to the first gap of a new shock, and it stays low through the fast rebound that often follows, which is where the strategy gives return back to buy-and-hold.

**Decision frequency.** Once per regular session, on the last 1-minute bar: minute 959 (15:59), or minute 779 (12:59) on short sessions. The order fills at the next session’s open (minute 570, 09:30), including the overnight gap. Every regular session is eligible, short sessions included. Extended hours are not traded.

**Rules.** After 10 session returns exist, target = min(1.0, 0.10 / max(RV, 0.06)), rounded to the nearest 0.05, and sent only when it differs from the last committed target by at least 0.10. Before that, target 0. The book is long-only. There is no short rule, no trend filter, and no intraday stop in the frozen code.

**Indicators.** One: 10-session realized volatility. RV is the sample standard deviation of the last 10 regular-session close-to-close returns, annualized with sqrt(252). Session close is the close of the last regular-session bar.

**Sizing.** The target is a fraction of equity in [0, 1]. The engine converts it to whole shares at the next open, `trunc(target * equity_at_open / open)`, and does not rebalance while the target is unchanged. Marked exposure drifts slightly between resizes.

**Risk controls.** Hard cap at 1.0 (the account allows 2.0). Vol floor at 6% so a flat print cannot force the cap. 0.10 deadband limits turnover to about 7x equity per year. Realized full-sample volatility landed at 10.7% versus the 10% target. There is no drawdown kill-switch in the code. A human overlay, not implemented, would flatten the book if account drawdown passed 30%, or if the next open was not a real auction (halt, corporate action, empty book).

**Variants.** 697 evaluated on the design window (days 0–2999): 28 exploratory moving-average rules, 658 in the formal grid, 9 intraday-stop add-ons, 1 constant-weight control, 1 overnight-only cost check. Walk-forward: train sessions 252–2199, design holdout 2200–2999 consulted only for the top 8 train names. Validation (days 3000–3795) was run once after the rule was frozen.

|  | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 236.4% | 434.2% | 122.8% | 193.9% | 51.0% | 81.7% |
| CAGR | 8.39% | 11.77% | 6.96% | 9.48% | 13.93% | 20.82% |
| Ann. vol | 10.74% | 20.00% | 10.79% | 20.73% | 10.54% | 17.00% |
| Sharpe | 0.804 | 0.656 | 0.678 | 0.541 | 1.291 | 1.198 |
| Sortino | 1.126 | 0.932 | 0.952 | 0.770 | 1.789 | 1.680 |
| Max drawdown | −20.89% | −53.50% | −20.89% | −53.50% | −12.02% | −23.14% |
| DD duration (sessions) | 600 | 798 | 600 | 798 | 159 | 157 |
| Calmar | 0.401 | 0.220 | 0.333 | 0.177 | 1.159 | 0.900 |
| Round trips | — | — | — | — | — | — |
| Hit rate | — | — | — | — | — | — |
| Turnover / year | 7.05 | 0.03 | 7.35 | 0.05 | 6.46 | 0.00 |
| Fees | 31,332 | 294 | 26,531 | 294 | 2,155 | 0 |
| Slippage | 18,784 | 100 | 13,010 | 100 | 2,592 | 0 |
| Borrow | 0 | 0 | 0 | 0 | 0 | 0 |
| Avg. abs. exposure | 0.682 | 1.000 | 0.663 | 1.000 | 0.753 | 1.000 |

Round trips and hit rate are blank because the engine closes an episode only when the book flats or flips. This strategy goes long on session 11 and never flats or shorts, so the episode is still open at the sample end. Buy-and-hold is the same shape with a single entry. Official outputs are in `results/`.
