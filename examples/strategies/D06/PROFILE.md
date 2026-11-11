# ASSET profile — volatility budget

**Lens.** Started as regularized ML on engineered features. Walk-forward ridge forecasts of the next session had out-of-sample correlation about 0.02 and negative R². What held up is a constant premium sized by a causal variance forecast. The live rule is that model.

**Thesis.** Session returns carry a positive premium and volatility clusters. Hold the premium at full weight when blended realized volatility is at or below 18% a year, and hold less when it is higher. Fails in a long moderate-vol bear, on the first gap out of a calm market, and on the rebound while trailing volatility is still high.

**Decision frequency.** Once per regular session, at the last bar (minute 959, or 779 on a short session). Fills at the next session's open (minute 570). About four fills a year on the design window.

**Sessions.** Regular session only, including short sessions. Extended hours are off.

**Rules.** Flat until 63 session returns exist. Then `rv = 0.5 * std_21 + 0.5 * std_63`, annualized with `sqrt(252)`, and `target = clip(0.18 / rv, 0, 1)`. Send the target, rounded to 0.01, only when it has moved by at least 0.10. Never short. Never above 1×.

**Indicators.** Session close-to-close return; 21-session realized vol; 63-session realized vol; their equal-weight blend. No price trend, weekday, volume, or overnight input in the live rule.

**Sizing.** Target is a fraction of equity. The engine trades whole shares at the next open and does not rebalance an unchanged target for drift.

**Risk controls.** Cap 1.0, floor 0, 0.10 band, 63-session warm-up flat, regular hours only. No coded drawdown kill. Planning drawdown is the design maximum, about −34% and roughly two years. Worst observed session −4.97% versus −9.04% for the asset.

**Variants.** 1,257 unique sets on the design window (days 0–2999), with folds 252–999, 1000–1699, 1700–2399 and a holdout at 2400–2999. Validation (3000–3795) was run once after the freeze.

| Metric | Design strat | Design B&H | Valid. strat | Valid. B&H | Full strat | Full B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 243.6% | 193.9% | 67.6% | 81.7% | 476.0% | 434.2% |
| CAGR | 10.9% | 9.5% | 17.8% | 20.8% | 12.3% | 11.8% |
| Ann. vol | 15.6% | 20.7% | 14.7% | 17.0% | 15.4% | 20.0% |
| Sharpe | 0.742 | 0.541 | 1.184 | 1.198 | 0.831 | 0.656 |
| Sortino | 1.05 | 0.77 | 1.63 | 1.68 | 1.17 | 0.93 |
| Max DD | −33.6% | −53.5% | −18.6% | −23.1% | −33.6% | −53.5% |
| DD length | 541 | 798 | 160 | 157 | 541 | 798 |
| Calmar | 0.33 | 0.18 | 0.95 | 0.90 | 0.37 | 0.22 |
| Turnover / yr | 0.50 | 0.05 | 0.78 | 0 | 0.61 | 0.03 |
| Fees | $2,411 | $294 | $265 | $0 | $3,322 | $294 |
| Slippage | $1,111 | $100 | $330 | $0 | $2,244 | $100 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |
| Avg exposure | 0.88 | 1.00 | 0.92 | 1.00 | 0.89 | 1.00 |
| Bars long / short | 98% / 0 | 100% / 0 | 100% / 0 | 100% / 0 | 98% / 0 | 100% / 0 |
| Round trips | 0 | 0 | 0 | 0 | 0 | 0 |

Round trips are zero because the position is one continuous long after warm-up; fills are rebalances inside that long. Hit rate is undefined for the same reason, for the strategy and for buy-and-hold.
