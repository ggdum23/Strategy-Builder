# Profile — long or flat, decided before the close

**Lens.** End of the regular session, about 30 minutes before the close, and the night that follows. The last hour is not a standalone signal. Its drift is flat to slightly negative, and a daily round trip costs more than the overnight premium (about +3.4 bp gross versus roughly 5–7 bp all-in). The decision is still made in that window so a new long earns the coming night and a new flat skips it.

**Thesis.** The asset's clean drift is overnight, and it persists for months at a time. A vote of three slow trends (60, 120, 252 sessions) is long when at least two are up and flat otherwise. Fast momentum mean-reverts here and was rejected. The book fails when a bull market rises on exactly the days the vote is off (the validation window), and when a gap hits before 15:29 while the vote is still long.

**Decision frequency.** Once per regular session. Full session: bar 15:29, fill at the 15:30 open. Shortened session: bar 12:29, fill at the 12:30 open. Every other bar returns `None`. Extended hours are not used. All 93 historical fills were at minute 930.

**Rules.** Let P(L) be the decision price divided by the session close L sessions ago, minus one. Horizons count only after L closes are stored (flat for the first 120 sessions). Target +1 if at least two available horizons are positive, else 0. No shorts, no second overlay, no resize until the vote changes.

**Indicators.** P(60), P(120), P(252) only. Zero threshold. Closes are stored in `on_day_end`, after the decision, so today's official close is not in the denominator.

**Sizing.** Target 0 or 1 times equity. Whole shares, trunc toward zero, at the fill open. No drift rebalance. Peak close-of-session weight observed: 1.0005. Average absolute exposure 0.79 full sample. The 2x cap is unused.

**Risk controls.** The vote is the de-risk rule. Cash earns zero and pays no borrow. Vol caps, a "down 1.5% by 15:30, go flat" stress rule, and leverage of 1.25–1.5 were tested and rejected (turnover or a deeper drawdown, no stable Sharpe gain). There is no in-code equity halt.

**Variants.** 139 evaluated, all on days 0–2999. Ranked on three walk-forward blocks from day 504 (score = mean Sharpe + 0.15 × worst block). A fourth design block was confirmation only. Validation was one official run after the file was frozen. The design-window engine matched the research simulator to the cent before that run.

| | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 426.8% | 434.2% | 231.9% | 193.9% | 58.7% | 81.7% |
| CAGR | 11.66% | 11.77% | 10.60% | 9.48% | 15.75% | 20.82% |
| Vol | 14.1% | 20.0% | 13.9% | 20.7% | 14.8% | 17.0% |
| Sharpe | 0.85 | 0.66 | 0.79 | 0.54 | 1.06 | 1.20 |
| Max drawdown | −19.2% | −53.5% | −19.2% | −53.5% | −15.5% | −23.1% |
| Calmar | 0.61 | 0.22 | 0.55 | 0.18 | 1.02 | 0.90 |
| Turnover / yr | 5.95 | 0.03 | 6.53 | 0.05 | 5.01 | 0 |
| Bars long / flat | 79% / 21% | 100% / 0 | 75% / 25% | 100% / 0 | 92% / 8% | 100% / 0 |

Validation lagged buy-and-hold because 68 sessions were entered flat and the asset compounded +13.4% on those sessions. On sessions entered long, the two books matched. Drawdown stayed shallower. No parameter was changed after seeing this.
