# Profile

**Lens.** Session structure. On the design window the close-to-open drift is about +3.4 bp per session (Sharpe 0.76) and the open-to-close drift is about +1.1 bp (Sharpe 0.15). They are almost uncorrelated. A book that owns only the overnight gap loses money after costs, because a round trip costs more than the gap.

**Thesis.** Collect the overnight premium by holding the regular-session close whenever the strategy is invested. Step aside only when that hold is underwriting clustered gap risk: the decision price is below its 180-session average and 20-session realized volatility is above 20%.

**Decision frequency.** Once per regular session, on the bar that starts at 15:58 (minute 958), or 12:58 (minute 778) on a short session. The fill is the next bar's open, still before the close, so a new target is on for the close and the overnight.

**Sessions.** Regular session only. Pre-session and post-session bars are not received and not traded. A long target is held overnight. A flat target is flat overnight.

**Rules.** One rule, in order:

1. Until 180 decision prices exist, target **+1**.
2. If the decision price is below the 180-session mean and annualized 20-session realized vol is above 0.20, target **0**.
3. Otherwise target **+1**.

No shorts. No leverage. An unchanged target does not rebalance.

**Indicators.** Decision price \(P_t\) (close of the decision bar). \(MA_t = \mathrm{mean}(P_{t-179..t})\). \(RV_t = \mathrm{stdev}_{n-1}(P_s/P_{s-1}-1,\ s=t-19..t)\times\sqrt{252}\).

**Sizing.** Engine whole shares: \(\mathrm{trunc}(\mathrm{target}\times\mathrm{equity}/\mathrm{fill\ price})\) at the next open, with 1 bp adverse slippage. Target is 0 or 1. Cash is the truncation residual, so close exposure stays near 0 or near 1.

**Risk controls.** The vol-and-trend gate is the de-risk rule. Realized full-sample volatility is 14.5% (buy-and-hold 20.0%). Maximum close exposure is 1.00. Flat about 14.5% of sessions, long the rest, short never. Observed maximum drawdown −28.8% over 541 sessions.

**Variants.** 257 distinct rules on the design window, plus the buy-and-hold benchmark. Validation (days 3000–3795) was run once after the rule was frozen.

| | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 375.6% | 434.2% | 174.3% | 193.9% | 73.4% | 81.7% |
| CAGR | 10.9% | 11.8% | 8.8% | 9.5% | 19.0% | 20.8% |
| Ann. vol | 14.5% | 20.0% | 14.5% | 20.7% | 14.5% | 17.0% |
| Sharpe | 0.785 | 0.656 | 0.656 | 0.541 | 1.276 | 1.198 |
| Max drawdown | −28.8% | −53.5% | −28.8% | −53.5% | −14.1% | −23.1% |
| Calmar | 0.38 | 0.22 | 0.31 | 0.18 | 1.35 | 0.90 |
| Turnover / year | 3.43 | 0.03 | 4.76 | 0.05 | 1.29 | 0 |
