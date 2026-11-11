# PROFILE — ASSET, volatility-scaled long

**Lens.** Started from one-minute range, trade count and volume shocks. Those features do not pay as entry signals after 1 bp slippage and per-share fees (a few basis points of reversal against a round trip of about 6–7 bp). The book that survived the design window uses session closes only: a 15-session variance scale and a 200-session average.

**Thesis.** ASSET has a positive drift and volatility that clusters. Scaling exposure to a 20% volatility target cuts the book when recent variance is high and allows modest leverage when it is calm. A slow mean-reversion tilt trims exposure when price is extended above its 200-session average and adds it when price is depressed. The tilt is clipped so a trend cannot flatten the book or double it.

**Decision frequency.** Once per regular session, on the closing bar: minute 959, or minute 779 on a short session. The fill is the next session's open. Extended hours are not used. Every delivered RTH session is eligible, including short sessions.

**Rules.** Stay flat until 200 completed closes. Then

- `rv` = sample standard deviation of the last 15 close-to-close returns, times `sqrt(252)`
- `base` = `min(1.75, 0.20 / rv)`
- `dist` = `close / SMA(200) - 1`
- `mult` = `clip(1 - 5 * dist, 0.50, 1.50)`
- `desired` = `clip(base * mult, 0, 1.75)`

Send a new target only if it differs from the live target by more than 0.12. Otherwise keep the current target. Long or flat only; no shorts.

**Indicators.** 15-session realized volatility (close-to-close, annualized). 200-session simple average of session closes. Distance of the latest close from that average.

**Sizing.** Target is a fraction of equity, capped at 1.75 (engine hard cap is 2). Whole shares are chosen by the engine at the fill. Average absolute exposure is about 0.85–0.89. Exposure is above 1 on roughly 28% of design sessions and above 1.5 on about 4%. It can drift a little above 1.75 between rebalances (design maximum mark 1.77).

**Risk controls.** The volatility scale is the de-risk rule. The 1.75 cap and the 0.50–1.50 multiplier clip are hard. No short borrow. No discretionary path in code. Flat for the first 200 sessions.

**Variants.** 763 design-window parameterizations were scored (299 in the broad screen, 455 in the refinement grid, 9 range/volume volatility overlays). Validation was run once after the rule was frozen.

## Metrics vs buy-and-hold

Official engine, $1,000,000 start, 1 bp slippage. Round trips are 0 because the position, once opened, never returns to flat; `n_fills` is the rebalance count.

| | Design strat | Design B&H | Valid strat | Valid B&H | Full strat | Full B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 267.0% | 193.9% | 85.6% | 81.7% | 581.0% | 434.2% |
| CAGR | 11.54% | 9.48% | 21.62% | 20.82% | 13.58% | 11.77% |
| Ann. vol | 17.64% | 20.73% | 14.57% | 17.00% | 17.04% | 20.00% |
| Sharpe | 0.707 | 0.541 | 1.417 | 1.198 | 0.833 | 0.656 |
| Sortino | 1.019 | 0.770 | 2.062 | 1.680 | 1.201 | 0.932 |
| Max DD | -39.91% | -53.50% | -20.66% | -23.14% | -39.91% | -53.50% |
| DD sessions | 742 | 798 | 112 | 157 | 742 | 798 |
| Calmar | 0.289 | 0.177 | 1.047 | 0.900 | 0.340 | 0.220 |
| Turnover / yr | 7.90 | 0.05 | 6.08 | 0 | 7.13 | 0.03 |
| Fees | $32,704 | $294 | $2,214 | $0 | $40,827 | $294 |
| Slippage | $17,412 | $100 | $2,696 | $0 | $27,307 | $100 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |
| Avg \|exposure\| | 0.852 | 1.000 | 0.888 | 1.000 | 0.859 | 1.000 |
| Final equity | $3.670M | $2.939M | $1.856M | $1.817M | $6.810M | $5.342M |

Validation buy-and-hold shows zero fees because its single entry happened in the 504-session warm-up, before the account reset. The strategy keeps trading inside the window.
