# Profile

**Lens.** Research started with an opening-range breakout. On the design selection sample the breakout's gross follow-through was about zero, and a round trip costs several basis points, so that lens was dropped. The shipped rule is a volatility-managed long.

**Thesis.** Session-to-session volatility clusters, and the deep losing stretches in this sample arrived while realized volatility was already high. Scaling a long book down in those stretches, and up when the last 20 sessions were quiet, keeps the asset's positive drift with a tighter equity curve. It lags when a rally resumes just after volatility has spiked, and it is fully exposed to a gap that starts from a quiet, 1.5x book.

**Decision frequency.** Once per regular session, on the last bar (minute 959, or minute 779 when `short_session` is 1). The fill is the next session's open. Extended hours are not used.

**Sessions.** Every regular session, including shortened ones. Flat only while the 20-session window is still forming, or when realized volatility is at or above 60% and the size step rounds to 0.

**Rules.** Let \(C_t\) be the session close and \(r_t = C_t/C_{t-1}-1\). Realized vol is the sample standard deviation of the last 20 session returns, annualized with \(\sqrt{252}\). Raw size is \(\min(1.5,\ 0.15/rv)\), then rounded to the nearest 0.5 with half-to-even (`numpy.round`) and clipped to [0, 1.5]. A new target is sent only when that step changes. The book is never short.

**Indicators.** One: 20-session close-to-close realized volatility. No opening range, moving average, or momentum input remains in the live rule.

**Sizing.** Targets are 0, 0.5, 1.0, or 1.5. In exact arithmetic those buckets are rv ≥ 0.60, (0.20, 0.60), [0.12, 0.20], and rv < 0.12. Whole shares, `trunc` toward zero, at the next open. The same target is not rebalanced for price drift, so close exposure can sit a little above 1.5 (the observed maximum is 1.54).

**Risk controls.** Hard cap 1.5 (the engine allows 2). Flat above roughly 60% realized vol. No shorts, so no borrow. One decision a day, so turnover stays near 8× equity per year. A human might cut a 1.5× book after a gap of several percent against the position; that override is not in the code.

**Variants.** 1,125 costed design-window variants (859 in the first grid, 266 new ones in the plateau check), plus the buy-and-hold benchmark. Days 252–2399, four blocks, were used to select. Days 2400–2999 were one confirmation look. Validation days 3000–3795 were run once after the rule was frozen.

| | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 467.7% | 434.2% | 208.8% | 193.9% | 83.8% | 81.7% |
| CAGR | 12.22% | 11.77% | 9.93% | 9.48% | 21.26% | 20.82% |
| Vol | 16.11% | 20.00% | 16.17% | 20.73% | 15.85% | 17.00% |
| Sharpe | 0.797 | 0.656 | 0.667 | 0.541 | 1.296 | 1.198 |
| Max drawdown | −26.66% | −53.50% | −26.66% | −53.50% | −16.58% | −23.14% |
| Calmar | 0.458 | 0.220 | 0.373 | 0.177 | 1.282 | 0.900 |
| Turnover / year | 7.91 | 0.03 | 9.11 | 0.05 | 6.10 | 0.00 |
| Avg \|exposure\| | 0.993 | 1.000 | 0.960 | 1.000 | 1.114 | 1.000 |
