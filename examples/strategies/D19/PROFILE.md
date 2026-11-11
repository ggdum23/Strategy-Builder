# Profile — ASSET vol-managed long

**Lens.** Started as an ensemble of simple trend and momentum votes. Those votes cut drawdowns and gave up too much of the drift. The rule that won the design walk-forward is a single scaler: 20-session realized volatility, long only.

**Thesis.** Daily drift is positive and volatility clusters. Cutting exposure after realized vol rises keeps more of the compounding through a high-vol decline than it gives up on the high-vol rebound. It lags when violent tapes are also strong advances, and it eats the first gap out of a calm window.

**Decision frequency.** Once per regular session, at minute 950 (15:50) or minute 770 (12:50) on a short session. The fill is the next bar’s open, ten minutes before the close. Extended hours are not used.

**Sessions.** Every regular session, including short sessions. Flat until 20 completed sessions are in memory. The live runner’s warm-up covers that.

**Rules.** Let σ̂ be the annualized sample standard deviation of the last 20 close-to-close returns, the newest one using the decision price. Target = min(1, 0.18 / σ̂), rounded to {0, 0.25, 0.50, 0.75, 1.00}, halfway cases up. Above the current target the engine buys; below it, the engine sells; unchanged, it stands by. No short.

**Indicators.** One: 20-session close-to-close realized variance, divided by 19, annualized with √252.

**Sizing.** The returned number is a fraction of equity, cap 1. Shares = trunc(target × equity_at_open / open). Average close exposure is about 0.90. Marked exposure peaks at 1.0005. Never short.

**Risk controls.** Quarter-turn grid, 1x cap, flat only in warm-up or at absurd vol (above ~144%). No equity stop, because the account is reset after warm-up and strategy memory is not. Stop trading if a multi-year high-vol rally trails buy-and-hold by ~15 percentage points with no drawdown benefit, if strategy vol stays above ~25% for a year while the book is already at half size and the drawdown pushes through −40%, or if the session close moves away from the decision minute.

**Variants.** 158 fully backtested design-window rules (94 screen, 63 refinement, 1 post-plateau ensemble). Walk-forward score was Sharpe on sessions 756–2999. Validation was run once after the freeze.

| | Design | Design B&H | Validation | Valid. B&H | Full | Full B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 221.9% | 193.9% | 79.7% | 81.7% | 478.4% | 434.2% |
| CAGR | 10.32% | 9.48% | 20.39% | 20.82% | 12.36% | 11.77% |
| Vol | 15.6% | 20.7% | 14.2% | 17.0% | 15.3% | 20.0% |
| Sharpe | 0.707 | 0.541 | 1.376 | 1.198 | 0.837 | 0.656 |
| Sortino | 0.995 | 0.770 | 1.926 | 1.680 | 1.177 | 0.932 |
| Max DD | −33.4% | −53.5% | −14.6% | −23.1% | −33.4% | −53.5% |
| DD sessions | 550 | 798 | 138 | 157 | 550 | 798 |
| Calmar | 0.31 | 0.18 | 1.40 | 0.90 | 0.37 | 0.22 |
| Turnover / yr | 2.24 | 0.05 | 1.64 | 0 | 2.00 | 0.03 |
| Fees + slip + borrow | $14.9k | $0.4k | $1.3k | $0 | $19.0k | $0.4k |
| Avg. abs. exposure | 0.90 | 1.00 | 0.93 | 1.00 | 0.90 | 1.00 |
