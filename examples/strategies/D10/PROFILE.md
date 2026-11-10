# Profile

**Lens.** Unconstrained. The design-window evidence did not support a directional timing model after costs. What survived was a long-only volatility throttle.

**Thesis.** ASSET’s variance clusters and is not paid for by a higher mean. Hold a long book, and cut it from 1× toward 0.25× only after an EWMA of close-to-close variance has risen. Do not lever, and do not short: leverage amplified calm-market drawdowns, and the short side gave up the drift.

**Decision frequency.** Once per regular session, on the last bar (minute 959, or 779 when `short_session` is set). The order fills at the next session’s open (minute 570). Extended hours are not used.

**Rules.** After 20 close-to-close returns, annualized EWMA volatility (λ = 0.94, 252 sessions) is σ. Raw exposure is min(1, 0.18 / max(σ, 0.05)). The live target stays put until the raw value differs by at least 0.25, then it snaps to the nearest quarter in {0, 0.25, 0.5, 0.75, 1}. In practice the book is flat only during that 20-session seed; afterwards it is always long. One rule, no override stack.

**Indicators.** Close-to-close simple return; RiskMetrics EWMA variance; the 0.25 hysteresis band. No price moving average in the shipped rule.

**Sizing.** `shares = trunc(target × equity_at_next_open / next_open)`. Target is the whole position, not a delta. The engine does not rebalance drift.

**Risk controls.** Hard cap at 1×, hard floor at 0 after the seed only in the limit of infinite vol (observed floor 0.25), no borrow, resize only when the band breaks. A human would pause the book if its own drawdown passed 40%, or if resizes suddenly became intraday. That pause is not in the code.

**Variants.** 420 costed specifications on the design window, plus 8 intraday checks that were cheaper to reject on gross edge than to backtest. Validation (sessions 3000–3795) was run once, after the file was frozen.

| | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 432.8% | 434.2% | 208.1% | 193.9% | 72.9% | 81.7% |
| CAGR | 11.75% | 11.77% | 9.91% | 9.48% | 18.93% | 20.82% |
| Vol | 15.58% | 20.00% | 15.75% | 20.73% | 14.91% | 17.00% |
| Sharpe | 0.791 | 0.656 | 0.679 | 0.541 | 1.238 | 1.198 |
| Max drawdown | −36.2% | −53.5% | −36.2% | −53.5% | −18.5% | −23.1% |
| Calmar | 0.324 | 0.220 | 0.274 | 0.177 | 1.023 | 0.900 |
| Turnover / yr | 0.49× | 0.03× | 0.40× | 0.05× | 0.63× | 0 |
