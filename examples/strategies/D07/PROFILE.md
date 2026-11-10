# ASSET profile

**Lens.** Volume / VWAP. The traded signal is where the regular-session close sits relative to that session's VWAP. Abnormal-volume filters and intraday VWAP fades were tested and dropped.

**Thesis.** A close at least 20 bps under session VWAP is a cheap finish versus the price where volume traded. That dislocation has been followed by higher subsequent sessions often enough, after costs, to beat buy-and-hold on Sharpe and drawdown over the full pack. The position is held until a later close finishes at least 40 bps rich to its own VWAP. Rich closes are not shorted: that side flipped sign inside the design window.

**Decision frequency.** Once per regular session, at the last bar: minute 959 (15:59), or minute 779 (12:59) on a short session. Fill is the next session's 09:30 open. Extended hours are not traded.

**Rules.** One bit, long or flat.

1. Flat and `(close − session_vwap) / close ≤ −0.002` → target +1.
2. Long and deviation `≥ +0.004` → target 0.
3. Otherwise keep the current target. No order.

`session_vwap = Σ (bar wap × volume) / Σ volume` over the current regular session, including the decision bar.

**Indicators.** That deviation only. Lookback is the current session. No moving average, vol estimate, or volume z-score in the live rule.

**Sizing.** Target 0 or +1 times equity. Whole shares at the fill open via the engine (`trunc`). No short, no leverage above 1, no rebalance while the target is unchanged. Realized close exposure stays at or just under 1 while long. Average absolute exposure is about 0.60 on the full sample (long ~60% of sessions, flat ~40%, short 0%).

**Risk controls.** The entry and the rich-close exit are the controls. No stop, no equity kill-switch, no vol cap: each of those failed a pre-set worst-fold test on the design window (the 15% kill-switch sat out the recovery). Worst full-sample day −7.92% (day 974). Max drawdown −19.68%. Longest time under a prior peak: 330 sessions (depth of that stretch only −10.4%). Deepest hole lasted 140 sessions.

**Variants.** 263, all chosen from days 0–2999 (212 costed screen, 14 size/filter/neighbor, 13 engine overlays, 24 intraday buckets). Validation days 3000–3795 were run once after the file was frozen.

| Metric | Design strat | Design B&H | Valid. strat | Valid. B&H | Full strat | Full B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 386.47% | 193.93% | 60.51% | 81.75% | 681.09% | 434.21% |
| CAGR | 14.21% | 9.48% | 16.16% | 20.82% | 14.62% | 11.77% |
| Ann. vol | 15.65% | 20.73% | 13.91% | 17.00% | 15.30% | 20.00% |
| Sharpe | 0.928 | 0.541 | 1.147 | 1.198 | 0.969 | 0.656 |
| Sortino | 1.384 | 0.770 | 1.639 | 1.680 | 1.434 | 0.932 |
| Max drawdown | −19.68% | −53.50% | −13.58% | −23.14% | −19.68% | −53.50% |
| Max DD duration | 330 | 798 | 123 | 157 | 330 | 798 |
| Calmar | 0.722 | 0.177 | 1.190 | 0.900 | 0.743 | 0.220 |
| Round trips | 347 | 0 | 54 | 0 | 401 | 0 |
| Hit rate | 64.0% | — | 64.8% | — | 64.1% | — |
| Hold (sessions) | 4.95 | — | 9.89 | — | 5.68 | — |
| Turnover / year | 53.79 | 0.05 | 34.81 | 0 | 46.15 | 0.03 |
| Fees | $306,963 | $294 | $11,051 | $0 | $358,711 | $294 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |
| Avg abs. exposure | 0.582 | 1.00 | 0.689 | 1.00 | 0.604 | 1.00 |
