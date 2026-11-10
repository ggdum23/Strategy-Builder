# Profile — vol-budget overlay

**Lens.** Volatility regimes: realised-volatility level as the risk budget. Vol clustering is real on this history; the conditional mean of the next session is not a reliable function of the vol level. The rule therefore sizes risk, it does not forecast direction.

**Thesis.** Close-to-close vol clusters (absolute-return autocorrelation about 0.23–0.28 on the design window). The worst sessions arrived when blended vol was already high, so cutting gross exposure after vol has risen trims the rest of a cluster. Capping exposure at 1x, rather than levering the quiet periods, is deliberate: 1.5–2x budgets improved the selection window and then drew down harder than buy-and-hold on the untouched design slice (days 2520–2999).

**Decision frequency.** Once per regular session, on the last bar (minute 959, or 779 on a shortened session). The order fills at the next session's 09:30 open. No extended hours.

**Sessions.** Every regular session, including shortened ones. Flat only before the first fill.

**Rules.** Let RV be the average of 20-session and 60-session close-to-close realised volatility (sample stdev, annualised by sqrt(252), including today's close). Target = nearest quarter of `clip(0.18 / RV, 0, 1)`. Send an order only when that quarter changes. Until 60 closes exist, hold 1.0. No shorts, no second signal, no stop outside this formula.

**Indicators.** Session close-to-close return; 20-session realised vol; 60-session realised vol; their average. Nothing else.

**Sizing.** The target is a fraction of equity. The engine buys or sells `trunc(target * equity_at_open / open)` whole shares, with 1 bp adverse slippage, the pack fee schedule, and no borrow because the book is never short. Live gross stays at or microscopically above the target only because an unchanged target is not rebalanced for price drift. The cap is 1.0. In the design window the decided weight was 1.00 on 76% of sessions, 0.75 on 16%, 0.50 on 7%, and 0.25 on 2%.

**Risk controls.** The cap, the quarter step, and the one-notch rebalance band. Weight steps down through about 21%, 29%, and 48% blended vol. There is no separate equity kill switch. A human overlay, not in the code, would flatten on a gap beyond about 8% or a halt.

**Variants.** 708 backtests on the design window (690 in the first grid, 18 corrected vol ladders). Validation (days 3000–3795) was run once after the rule was frozen and was not used to change it.

| | Full strat | Full B&H | Design strat | Design B&H | Valid. strat | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 443.8% | 434.2% | 212.8% | 193.9% | 73.9% | 81.7% |
| CAGR | 11.9% | 11.8% | 10.1% | 9.5% | 19.1% | 20.8% |
| Ann. vol | 15.6% | 20.0% | 15.8% | 20.7% | 14.7% | 17.0% |
| Sharpe | 0.80 | 0.66 | 0.68 | 0.54 | 1.26 | 1.20 |
| Sortino | 1.12 | 0.93 | 0.97 | 0.77 | 1.75 | 1.68 |
| Max drawdown | −32.6% | −53.5% | −32.6% | −53.5% | −16.7% | −23.1% |
| Calmar | 0.37 | 0.22 | 0.31 | 0.18 | 1.14 | 0.90 |
| Turnover / year | 1.23 | 0.03 | 0.97 | 0.05 | 1.63 | 0.00 |
| Avg. abs. exposure | 0.92 | 1.00 | 0.91 | 1.00 | 0.94 | 1.00 |

Sharpe uses 252 sessions and daily close-to-close equity marks, rf = 0, from the unmodified official runner. Round trips are zero in every window because the book never goes flat after entry and never flips short; the position is one continuous long that is resized.
