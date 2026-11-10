# PROFILE — Trailing-drawdown participation

**Lens.** Started as trailing-stop trend. Tight trend rules (SMA, Donchian, ATR chandelier) failed the design holdout, because design-window dips mean-revert. What held up is wide-band drawdown control on an always-long book.

**Thesis.** Own the drift. Go flat only after the close is 20% below the 100-session closing peak, and buy back on a 20-session closing breakout once price is back inside that band. The claimed edge is skipping a crash-sized decline. It gives back money when a decline only just breaks 20% and then recovers, which is what the validation window did.

**Decision frequency.** Once per regular session, on the last bar (minute 959, or 779 on a short session). Fill at the next session's open (minute 570).

**Sessions.** Regular session only. Extended hours are not read and not traded.

**Rules.** (1) Until 100 closes are in hand, target +1. (2) If long and close / 100-session peak − 1 ≤ −0.20, target 0. (3) If flat and the drawdown is back above −0.20 and the close is above the prior 20-session high, target +1. (4) Otherwise hold. No shorts.

**Indicators.** Session close; 100-session closing peak (today included); drawdown from that peak; prior 20-session closing high (today excluded).

**Sizing.** Target is exactly 0 or +1. Shares = trunc(target × equity / open) at the fill, then held until the target changes. No vol scale, no leverage above 1.

**Risk controls.** The 20% band is the stop. Re-entry needs a breakout, so a one-day bounce does not reload. Realized full-window vol 17.0% (buy-and-hold 20.0%). Max target leverage 1; max realized close exposure 1.0005. Flat about 4.8% of sessions, long the rest, short never. Observed max drawdown −30.4% over 467 sessions. Discretionary kill, not in the code: stop past a 40% equity drawdown, or if shallow 20% whipsaws start repeating.

**Variants.** 414 design-window configurations (177 family screen, 87 refinements and overlays, 144 default-long grid, 6 intraday-stop bounds). Validation was run once after the file was frozen and was not used to retune.

| | Full strategy | Full B&H | Design strategy | Design B&H | Valid. strategy | Valid. B&H |
|---|---:|---:|---:|---:|---:|---:|
| CAGR | 13.0% | 11.8% | 12.1% | 9.5% | 16.8% | 20.8% |
| Vol | 17.0% | 20.0% | 17.2% | 20.7% | 16.2% | 17.0% |
| Sharpe | 0.81 | 0.66 | 0.75 | 0.54 | 1.04 | 1.20 |
| Sortino | 1.14 | 0.93 | 1.06 | 0.77 | 1.42 | 1.68 |
| Max DD | −30.4% | −53.5% | −30.4% | −53.5% | −22.3% | −23.1% |
| Calmar | 0.43 | 0.22 | 0.40 | 0.18 | 0.75 | 0.90 |
| Turnover / yr | 0.43 | 0.03 | 0.31 | 0.05 | 0.59 | 0.00 |
| Fees | $3,032 | $294 | $2,185 | $294 | $219 | $0 |
| Borrow | $0 | $0 | $0 | $0 | $0 | $0 |

Full-window terminal equity $6.34M against $5.34M. The full-sample lead is one design-window crash (252-session block −23% against −45%). Validation lagged buy-and-hold after a single stop-and-re-entry.
