# PROFILE — calm-regime overlay

**Lens.** Regime switching. A two-state Gaussian HMM on daily returns (fit on design days 0–1499) finds a persistent calm state, about +8 bp/day at 16% vol, and a persistent stress state, about −20 bp/day at 44% vol. Trading that filter improves drawdown versus buy-and-hold and then loses to a nonparametric vol regime on the second half of the design window. The live rule is the nonparametric switch.

**Thesis.** On this instrument the close-to-close drift is earned unevenly. Absolute returns cluster, and the worst sessions sit in hot realized-vol regimes. Standing aside while 20-session vol is in the top fifth of its own trailing year keeps the long exposure in the calmer subset of sessions. The rule fails when the loss arrives as a gap out of a calm regime, when a bear market grinds lower without a vol spike, or when high vol is mostly upside and the book is flat through the advance.

**Decision frequency.** Once per regular session, on the last RTH bar (minute 959, or 779 on a short session). Fill at the next session’s open. No extended-hours trading. No intraday round trips.

**Rules.** Let RV20 be the sample standard deviation of the last 20 close-to-close returns, annualized by √252. Let the threshold be the 80th percentile of the last 252 RV20 readings. Raw state is calm when RV20 is strictly below the threshold, otherwise hot. The target flips only after the same raw state has printed for two sessions: calm → +1.0, hot → 0.0. Warmup is flat until 272 session closes exist. There is no short.

**Indicators.** Session close, 20-session realized volatility, trailing 252-session 80th percentile of that volatility, two-session confirmation streak.

**Sizing.** Target is 0 or 1 times equity. The engine trades whole shares, `trunc(target × equity_at_open / open)`, only when the target changes. Realized close exposure peaks at about 1.001 because fees leave a few dollars of negative cash. The ±2 allowance is unused. No borrow.

**Risk controls.** Flat in a confirmed hot-vol regime; two-session confirmation against one-bar whipsaws; hard cap at +1; flat until the vol window is full. A 35% live-drawdown review stop is discretionary and is not in the code.

**Variants.** 349 strategy configurations on the design window (days 0–2999), plus buy-and-hold. Validation (days 3000–3795) was run once after the file was frozen.

| | Full strat | Full B&H | Design strat | Design B&H | Valid. strat | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 550.5% | 434.2% | 280.9% | 193.9% | 70.8% | 81.7% |
| CAGR | 13.24% | 11.77% | 11.89% | 9.48% | 18.46% | 20.82% |
| Ann. vol | 14.27% | 20.00% | 14.73% | 20.73% | 12.35% | 17.00% |
| Sharpe | 0.943 | 0.656 | 0.836 | 0.541 | 1.434 | 1.198 |
| Max drawdown | −26.14% | −53.50% | −26.14% | −53.50% | −11.55% | −23.14% |
| Calmar | 0.51 | 0.22 | 0.45 | 0.18 | 1.60 | 0.90 |
| Turnover / yr | 5.03 | 0.03 | 5.98 | 0.05 | 3.62 | 0.00 |
