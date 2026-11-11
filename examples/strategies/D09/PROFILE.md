# Profile — ASSET defensive volatility scaler

**Lens.** Defensive capital preservation: cut exposure when losses cluster, keep the overnight premium the rest of the time, do not lever and do not short. A harder version (flat whenever vol is merely elevated) was the first choice and was dropped after it drew down more than buy-and-hold on the later design sessions.

**Thesis.** ASSET's average price gain is earned overnight. Intraday is approximately noise, and the ugly left tail arrives in clusters after realized volatility has already risen. Hold the close, at full size when trailing vol is calm and at half size when it is not. Go flat only in extreme vol. The cost is lagging buy-and-hold in a bull that never spikes vol. The payoff is a much smaller drawdown when a vol cluster does show up.

**Decision frequency.** Once per session, on the bar starting one minute before the regular close: minute 958 (15:58), or minute 778 (12:58) on a short session. Fill at the next bar's open (15:59 or 12:59), so the new size is held through the close and the overnight. No extended-hours trading. About 9–10 resizes a year on the design window; turnover 4.8x (design), 1.7x (validation), 3.6x (full).

**Sessions.** Every regular session, including short sessions. Pre/post bars are not requested.

**Rules.** Let \(P_t\) be the decision-bar close and \(r_t = P_t/P_{t-1}-1\). \(\sigma_t = \max(\sigma^{10}_t, \sigma^{40}_t)\) with sample standard deviation, annualized by \(\sqrt{252}\). Target, in priority order:

1. Fewer than 40 returns: **0**
2. \(\sigma_t \le 0.20\): **1.0**
3. \(\sigma_t \le 0.60\): **0.5**
4. Else: **0.0**

An unchanged target sends no order. These boundaries are the half-up 0.5-grid of \(\min(1,\ 0.15/\sigma)\).

**Indicators.** Only \(\sigma^{10}\) and \(\sigma^{40}\) of decision-price returns, and their max. No moving average, no intraday stop, no weekday rule.

**Sizing.** Whole shares at the fill: `trunc(target * equity / open)`, target in {0, 0.5, 1}. Gross cap 1x. No shorts. Average absolute exposure 0.80 design, 0.86 validation, 0.82 full. Max close-of-day exposure observed: 1.00.

**Risk controls.** The vol buckets above. No leverage, no short, no drift rebalance. Discretionary stops, not in the code: stand down past a 30% equity drawdown or an 8% one-session loss at full size; do not override a half-size state to buy a dip.

**Variants evaluated.** 702 (15 stop screens, 593-rule grid, 28 half-sample thresholds, 64 neighbor checks, 2 sketches). The shipped rule is the select-window (days 252–1511) Calmar leader among scalers with no hard flat. A hard-flat leader was precommitted, failed the internal confirm (1512–2015) and lockbox (2016–2999), and was replaced by this rule. Validation (3000–3795) was run once after the freeze.

**Metrics** (official engine, $1,000,000, 1 bp slippage):

| | Strat full | BH full | Strat design | BH design | Strat valid. | BH valid. |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 442.5% | 434.2% | 222.3% | 193.9% | 68.3% | 81.7% |
| CAGR | 11.9% | 11.8% | 10.3% | 9.5% | 17.9% | 20.8% |
| Ann. vol | 13.4% | 20.0% | 13.6% | 20.7% | 12.6% | 17.0% |
| Sharpe | 0.91 | 0.66 | 0.79 | 0.54 | 1.38 | 1.20 |
| Sortino | 1.28 | 0.93 | 1.12 | 0.77 | 1.90 | 1.68 |
| Max drawdown | −23.1% | −53.5% | −23.1% | −53.5% | −15.6% | −23.1% |
| Calmar | 0.51 | 0.22 | 0.45 | 0.18 | 1.15 | 0.90 |
| Worst day | −4.4% | −9.0% | −4.2% | −9.0% | −4.4% | −4.6% |
| Turnover / year | 3.61 | 0.03 | 4.79 | 0.05 | 1.67 | 0 |
| Avg exposure | 0.82 | 1.00 | 0.80 | 1.00 | 0.86 | 1.00 |
