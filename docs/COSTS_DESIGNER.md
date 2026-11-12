# Designer cost model

Designer-facing runs do not use the per-share fee schedules. They use a flat fee in basis points of traded notional, on every fill, plus the profile's existing slippage and the existing scale-invariant rates (borrow, financing, cash interest, dividends as rates).

The flat constants live in `src/backtester/costs_designer.py`. They are fixed per profile. They are not computed from a ticker, a price, a pack, or an account, so the same number applies to every symbol and carries no information about the real asset.

Designer dollar figures are an index. `DESIGNER_INITIAL_EQUITY` is `1000000`. It does not follow the operator account. Whole-share sizing uses that equity and the anonymized pack price.

The operator invoice is a different run. Arena still uses `costs.py` (IBKR Pro Tiered, TAF `0.000195` per share, cap `9.79`). Desk still uses `costs_desk.py` (SEC `27.80e-6`, TAF `0.000166` per share, cap `8.30`). `share_scale` is applied only on that operator run. `costs.py` is unchanged.

## What is charged

On a designer fill the fee is

```
abs(shares) * slipped_fill_price * flat_bps * fee_mult * 1e-4
```

`fee_mult` is the Desk stress switch. It scales this fee and the slippage bps the same way the Desk schedule scales commission dollars and slippage. Arena keeps it at 1. There is no per-share commission, no per-share SEC or TAF, and no minimum or maximum that depends on share count or price. Monthly volume is not consumed (`tier_shares` is 0).

Slippage stays the published 1 bp, adverse, applied to the fill price and reported separately. It is not inside the flat fee.

Borrow, financing, and cash interest stay the profile rates. Dividend cash on a designer run is the dividend-file amount times the pack share count. The amount is not multiplied by `share_scale`. Multiplying by the sidecar scale would put that scale into the designer result, and the designer tree has to be byte-identical across scales. The file amount is a rate in the units of the file, not a per-share regulatory schedule.

## Calibration

Calibrated once, offline, at a $100 stock and a 1.0x order on the canonical $1,000,000 index. That target is inside Arena's `[-2, 2]` band and Desk's `[-3, 3]` band.

```
shares  = int(1.0 * 1_000_000 / 100) = 10_000
notional = 10_000 * 100 = 1_000_000
```

Month volume before the fill is 0, so both schedules are on tier 1 (`$0.0035` per share). The commission minimum (`$0.35`), the commission cap (1% of value = `$10,000`), and the clearing cap (0.5% of value = `$5,000`) do not bind. The TAF caps do not bind either.

Shared on each side, from the IBKR Pro Tiered pieces both schedules publish:

```
commission   = 10_000 * 0.0035 = 35
exchange     = 10_000 * 0.0030 = 30
clearing     = 10_000 * 0.00020 = 2
pass-through = 35 * (0.000175 + 0.00056) = 35 * 0.000735 = 0.025725
CAT          = 10_000 * 0.000003 = 0.03
subtotal     = 67.055725
one-way bps  = 67.055725 / 1_000_000 * 10_000 = 0.67055725 bp
```

Pass-through is `commission * (PASS_THROUGH_NYSE + PASS_THROUGH_FINRA)`, the same product the fee functions compute. It is not `commission * 0.000175 + 0.00056`.

Arena sells add SEC and TAF from `costs.py`:

```
SEC = 0.0000206 * 1_000_000 = 20.60 = 0.206 bp
TAF = min(10_000 * 0.000195, 9.79) = 1.95 = 0.0195 bp
sell = 0.67055725 + 0.206 + 0.0195 = 0.89605725 bp
average of the buy and the sell = (0.67055725 + 0.89605725) / 2 = 0.78330725 bp
```

Desk sells add SEC and TAF from `costs_desk.py`:

```
SEC = 27.80e-6 * 1_000_000 = 27.80 = 0.278 bp
TAF = min(10_000 * 0.000166, 8.30) = 1.66 = 0.0166 bp
sell = 0.67055725 + 0.278 + 0.0166 = 0.96515725 bp
average of the buy and the sell = (0.67055725 + 0.96515725) / 2 = 0.81785725 bp
```

Each average is rounded half-up to 0.1 bp.

| Profile | Unrounded average | Flat fee |
| --- | --- | --- |
| Arena | 0.78330725 bp | `ARENA_FLAT_BPS = 0.8` |
| Desk | 0.81785725 bp | `DESK_FLAT_BPS = 0.8` |

The two constants are separate. They happen to print as the same tenth. Charging that average on both the buy and the sell makes a round trip line up with the real schedule's buy plus sell at this reference, when the minimums and caps do not bind. After rounding, a round trip is 1.6 bp of notional. The unrounded round trips are 1.5666145 bp (Arena) and 1.6357145 bp (Desk).

The label on designer metrics and the designer leaderboard is `designer cost model: 0.8 bp flat`.

## Where it runs

`run --pack`, walk-forward design on a pack, parallel on a pack, and anything written under `designer/` use this model. The operator files next to them keep the real schedule and the real share counts. `cost_comparison.json` is an operator file. It shows both results and the difference (designer minus real) so the flat fee can be compared with the invoice. `total_return` is the comparison that does not depend on account size. `final_equity` is in each book's own unit: the operator account, or the canonical index.
