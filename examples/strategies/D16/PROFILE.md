# Profile — dual-horizon trend stand-aside

**Lens.** Overnight drift versus intraday reversal. On the design window the close-to-open leg has Sharpe 0.76 and the open-to-close leg has Sharpe 0.15, and they are uncorrelated. Harvesting the overnight with a round trip every session loses money after costs (research Sharpe about −0.59). The overnight premium is also about zero when the 120-session return is negative. The book therefore holds through both legs in an uptrend and stands flat only in a confirmed downtrend.

**Thesis.** Be long the drift. Step aside when a half-year downtrend and a one-month downtrend agree. That cuts the design-window collapse (buy-and-hold −45% in the 252 sessions that contain it; this book −10%) without paying a daily round trip. It lags in a clean bull market: validation Sharpe 1.06 against 1.20 for buy-and-hold.

**Decision frequency.** Once per regular session, on the last bar: minute 959, or minute 779 on a shortened session. Fill at the next session's open. Extended hours are not traded.

**Rules.** After 121 session closes: target 0 if the 120-session return and the 20-session return are both negative, otherwise target +1. Before that, flat. No shorts, no leverage, no other indicator.

**Indicators.** \(C_t/C_{t-120}-1\) and \(C_t/C_{t-20}-1\), session closes, updated at the decision bar.

**Sizing.** Target is 0 or 1. Shares = `trunc(target × equity_at_open / open)`. Unchanged targets do not rebalance.

**Risk controls.** The flat signal is the only de-risk. Gross exposure ≤ 1 (largest marked close in sample 1.0005, from fee cash drag). Flat about 15% of all sessions, long the rest, short never. Observed max drawdown −27.1%. Longest time under a peak: 329 sessions. Worst day −4.65% (buy-and-hold −9.04%, a day this book was flat).

**Variants evaluated.** 83 distinct parameterisations on the design window, plus buy-and-hold. Validation (days 3000–3795) was run once after the rule was frozen.

| | Full, strat / B&H | Design, strat / B&H | Validation, strat / B&H |
|---|---:|---:|---:|
| Total return | +545% / +434% | +303% / +194% | +60% / +82% |
| CAGR | 13.2% / 11.8% | 12.4% / 9.5% | 16.1% / 20.8% |
| Ann. vol | 15.2% / 20.0% | 15.1% / 20.7% | 15.2% / 17.0% |
| Sharpe | 0.89 / 0.66 | 0.85 / 0.54 | 1.06 / 1.20 |
| Sortino | 1.27 / 0.93 | 1.21 / 0.77 | 1.45 / 1.68 |
| Max drawdown | −27.1% / −53.5% | −27.1% / −53.5% | −20.9% / −23.1% |
| Calmar | 0.49 / 0.22 | 0.46 / 0.18 | 0.77 / 0.90 |
| Round trips (closed) | 56 / 0 | 49 / 0 | 7 / 0 |
| Hit rate | 46% / — | 47% / — | 43% / — |
| Avg hold (sessions) | 56 / continuous | 48 / continuous | 99 / continuous |
| Turnover / year | 6.6 / 0.03 | 8.0 / 0.05 | 4.5 / 0 |
| Fees | $48.3k / $294 | $42.3k / $294 | $1.5k / $0 |
| Borrow | $0 / $0 | $0 / $0 | $0 / $0 |
| Avg \|exposure\| | 0.85 / 1.00 | 0.82 / 1.00 | 0.94 / 1.00 |
