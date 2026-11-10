# Profile — opening-gap absorption

**Lens.** Intraday mean reversion, kept only where it clears costs. Stretch-versus-VWAP fades do not. A large overnight down-gap, bought for the first 30 minutes of the regular session, does. The book otherwise holds the asset, because the drift is positive and a round trip every day costs more than that drift.

**Thesis.** The open can print through fair value when overnight news meets a thin auction. Depth comes back after the open, and a down-gap of 1.2% or more has, on this sample, given back several tens of basis points inside half an hour. Up-gaps do not reliably give back; that mirror was tested and dropped. The add should fail when the gap is a real repricing rather than a concession, which is exactly what the worst day in the pack was.

**Decision frequency.** Standing target +1. An order is sent only when the target changes: the opening buy, then on a signal day a buy after the 09:30 bar (fills at the 09:31 open) and a sale after the 10:00 bar (fills at the 10:01 open). About 9 signal days a year on the design window, 6 on the validation window. Regular session only, including shortened sessions. No extended hours.

**Rules.** Let G = session open / previous regular close − 1, known when the 09:30 bar has closed.

- G ≤ −1.2% and the 10:00 bar exists in the session: target = +2.
- At the 10:00 bar, or on the last bar if a boost is still on: target = +1.
- Otherwise, if the current target is not +1: target = +1. Every other bar: no order.

No short. No flat state after the first fill. Cap is 2.

**Indicators.** Previous regular close (last bar of the prior session). Today's regular open (open of minute 570). Gap as above. No moving average, no fitted weight.

**Sizing.** `trunc(target × equity at the fill open / fill open)`, target in {1, 2}. Whole shares, engine costs, 1 bp slippage. Overnight leverage is 1. Intraday leverage is 2 for 30 minutes on a signal day, which is about 0.3% of regular-session bars.

**Risk controls.** Time stop at 30 minutes. Hard cap at 2. Last-bar guard so a boost is not left on overnight. No stop-loss: a 1% adverse-excursion stop was tested and worsened the drawdown path. A human might skip a halt or a broken print, and might cut the core long in a drawdown past about 35%. That override is not in the code.

**Variants.** 252 costed backtests (including buy-and-hold) and 117 diagnostic signal tests. Total 369. Parameters chosen on design days 400–2199 among gap rules with turnover under 20 a year. Validation days 3000–3795 were run once after the freeze.

**Metrics vs buy-and-hold** (official runner, rf = 0, 252 sessions/year):

| | Full strat | Full B&H | Design strat | Design B&H | Valid. strat | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 668.6% | 434.2% | 311.4% | 193.9% | 86.8% | 81.7% |
| CAGR | 14.50% | 11.77% | 12.62% | 9.48% | 21.88% | 20.82% |
| Vol | 20.3% | 20.0% | 21.0% | 20.7% | 17.4% | 17.0% |
| Sharpe | 0.768 | 0.656 | 0.670 | 0.541 | 1.226 | 1.198 |
| Max drawdown | −45.3% | −53.5% | −45.3% | −53.5% | −22.3% | −23.1% |
| Calmar | 0.320 | 0.220 | 0.278 | 0.177 | 0.979 | 0.900 |
| Turnover / yr | 14.6 | 0.03 | 15.9 | 0.05 | 13.1 | 0 |
| Worst day | −11.3% | −9.0% | −11.3% | −9.0% | −4.65% | −4.65% |
