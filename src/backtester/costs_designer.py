
from __future__ import annotations

import math

ARENA_FLAT_BPS = 0.8
DESK_FLAT_BPS = 0.8

DESIGNER_INITIAL_EQUITY = 1_000_000.0

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
    name = str(profile).strip().lower()
    if name == "arena":
        return ARENA_FLAT_BPS
    if name == "desk":
        return DESK_FLAT_BPS
    raise ValueError(f"unknown profile {profile!r}")


def label(profile: str) -> str:
    return f"designer cost model: {flat_bps(profile):.1f} bp flat"


def is_cost_label(text: str) -> bool:
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
    return DesignerCostModel(flat_bps(profile), slippage_bps=SLIPPAGE_BPS, fee_mult=fee_mult)


class DesignerCostModel:

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
        rate = self.slippage_bps * self.fee_mult * 1e-4
        if shares_signed > 0:
            return price * (1.0 + rate)
        return price * (1.0 - rate)
