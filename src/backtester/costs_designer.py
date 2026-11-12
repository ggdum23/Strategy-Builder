"""Designer cost model.

A flat fee in basis points of traded notional, charged on every fill, plus
the profile's existing slippage. The basis-point constants are fixed per
profile. They are not computed from a ticker, a price, a pack, or an
account. See docs/COSTS_DESIGNER.md.

This module does not import the per-share schedules and does not read a
scale factor. Borrow, financing, cash interest, and dividends stay in the
engines as rates.
"""

from __future__ import annotations

import math

# One-way all-in fee at the documented reference, rounded half-up to 0.1 bp.
# The two profiles use different sell fees, so the unrounded averages differ.
# Both land on 0.8 bp at a tenth of a basis point. See docs/COSTS_DESIGNER.md.
ARENA_FLAT_BPS = 0.8
DESK_FLAT_BPS = 0.8

# Designer dollar figures are an index in this unit. It does not follow the
# operator account size. Whole-share sizing uses this equity and the pack price.
DESIGNER_INITIAL_EQUITY = 1_000_000.0

# Same 1 bp the Arena and Desk schedules publish. Duplicated so this module
# does not import those schedules. tests/test_designer_costs.py locks the match.
SLIPPAGE_BPS = 1.0

_FEE_KEYS = (
    "commission",
    "exchange",
    "clearing",
    "pass_through",
    "cat",
    "sec",
    "taf",
    "total",
)


def flat_bps(profile: str) -> float:
    """The fixed flat fee for ``arena`` or ``desk``."""
    name = str(profile).strip().lower()
    if name == "arena":
        return ARENA_FLAT_BPS
    if name == "desk":
        return DESK_FLAT_BPS
    raise ValueError(f"unknown profile {profile!r}")


def label(profile: str) -> str:
    """The sentence written on designer metrics and the designer leaderboard."""
    return f"designer cost model: {flat_bps(profile):.1f} bp flat"


def is_cost_label(text: str) -> bool:
    """True for the exact label this module writes."""
    if not isinstance(text, str):
        return False
    prefix = "designer cost model: "
    suffix = " bp flat"
    if not (text.startswith(prefix) and text.endswith(suffix)):
        return False
    body = text[len(prefix) : -len(suffix)]
    if body.count(".") != 1:
        return False
    try:
        value = float(body)
    except ValueError:
        return False
    return math.isfinite(value) and body == f"{value:.1f}"


def for_profile(profile: str, *, fee_mult: float = 1.0) -> "DesignerCostModel":
    """The model for one profile. ``fee_mult`` is the Desk stress switch."""
    return DesignerCostModel(flat_bps(profile), slippage_bps=SLIPPAGE_BPS, fee_mult=fee_mult)


class DesignerCostModel:
    """Flat bps of traded notional on buys and on sells.

    ``fee_mult`` scales this fee and the slippage bps the same way the Desk
    schedule scales commission dollars and slippage. It does not scale borrow,
    financing, or dividends. Arena runs keep it at 1.
    """

    def __init__(self, flat_bps: float, *, slippage_bps: float = SLIPPAGE_BPS, fee_mult: float = 1.0):
        bps = float(flat_bps)
        slip = float(slippage_bps)
        mult = float(fee_mult)
        if not math.isfinite(bps) or bps < 0.0:
            raise ValueError("flat bps must be a non-negative finite number")
        if not math.isfinite(slip) or slip < 0.0:
            raise ValueError("slippage bps must be a non-negative finite number")
        if not math.isfinite(mult) or mult <= 0.0:
            raise ValueError("fee_mult must be a positive finite number")
        self.flat_bps = bps
        self.slippage_bps = slip
        self.fee_mult = mult

    def fill_costs(self, shares_signed, price, month_shares_before=0.0):
        """Dollar fee for one fill. The third argument is ignored.

        The fee is ``abs(shares) * price * flat_bps * fee_mult * 1e-4``.
        ``price`` is the slipped fill price the engine passes in. There is
        no per-share commission, no per-share regulatory fee, and no min or
        max that depends on share count or price. Monthly volume is not
        consumed: ``tier_shares`` is 0.
        """
        del month_shares_before
        shares = abs(float(shares_signed))
        px = float(price)
        if shares == 0.0 or px == 0.0:
            fee = 0.0
        else:
            fee = shares * px * (self.flat_bps * self.fee_mult) * 1e-4
        parts = {key: 0.0 for key in _FEE_KEYS}
        parts["total"] = fee
        parts["counted_shares"] = 0.0
        parts["tier_shares"] = 0.0
        return parts

    def slipped_price(self, price, shares_signed):
        """Adverse slippage. Buys pay up, sells receive less."""
        rate = self.slippage_bps * self.fee_mult * 1e-4
        if shares_signed > 0:
            return price * (1.0 + rate)
        return price * (1.0 - rate)
