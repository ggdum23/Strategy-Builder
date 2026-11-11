"""Profile rules shown before a run.

Numbers come from ``backtester.profiles``. The sentences say what that profile's
engine does. They are not a second fee schedule.
"""

from __future__ import annotations

from ..profiles import get_profile

_FILL = {
    "next_delivered_bar": "Fills at the open of the next bar the strategy is shown.",
    "next_rth_open": "Fills at the next regular-session open, and a newer signal replaces one that has not filled.",
}

_WARMUP = {
    "rescale_keep_exposure": "Warm-up trades, then the account is rescaled to the starting equity and exposure is kept.",
    "flat_until_live": "Warm-up stays flat, and the book starts flat at the starting equity.",
}

_FEES = {
    "arena": "Arena uses its own fee schedule plus 1 bp of slippage per side, and it rejects a fee multiple other than 1.",
    "desk": "Desk uses its own fee schedule plus 1 bp of slippage, and the fee multiple scales commissions, fees, and slippage.",
}


def profile_rows(name: str) -> tuple[tuple[str, str], ...]:
    """Labeled rules for one profile. Unknown names raise, the same as the runner."""
    profile = get_profile(name)
    cap = f"{profile.max_abs_target:g}"
    rows = [
        ("Profile", f"{profile.name}."),
        ("Clamp", f"Target exposure is clamped to ±{cap}, and an unchanged target is a hold."),
        ("Fills", _FILL.get(profile.fill_venue, profile.fill_venue)),
        ("Fees", _FEES.get(profile.fee_table, profile.fee_table)),
        (
            "Borrow",
            f"Borrow is {profile.borrow_rate_annual:.2%} per year on short notional unless the run overrides it.",
        ),
        (
            "Financing",
            f"Financing is {profile.financing_rate_annual:.2%} per year"
            + (
                ", and Arena rejects any other rate."
                if profile.name == "arena"
                else " on borrowed gross exposure at each session boundary."
            ),
        ),
        (
            "Dividends",
            "Dividends are not credited." if not profile.dividends else "Dividends are cash from the dividend file.",
        ),
    ]
    if profile.margin_call_gross is None:
        rows.append(("Margin", "There is no margin call."))
    else:
        rows.append(
            (
                "Margin",
                f"Gross leverage above {profile.margin_call_gross:g}× queues a reduction to ±{cap} at the next regular open.",
            )
        )
    if profile.liquidation_hwm_fraction is None:
        rows.append(("Liquidation", "There is no forced liquidation."))
    else:
        rows.append(
            (
                "Liquidation",
                f"Equity below {profile.liquidation_hwm_fraction:.0%} of the high-water mark flattens the book and flags DISQUALIFIED.",
            )
        )
    rows.append(("Warm-up", _WARMUP.get(profile.warmup, profile.warmup)))
    rows.append(
        (
            "Share count",
            "Named files use share factor 1, and an anonymized pack keeps its scale in the sidecar.",
        )
    )
    rows.append(
        (
            "Benchmarks",
            "The benchmark is 1× buy and hold through the same engine and the same costs"
            + (", and Desk also runs a financed 2× long." if profile.name == "desk" else "."),
        )
    )
    return tuple(rows)


def profile_text(name: str) -> str:
    return "\n".join(f"{label}. {body}" for label, body in profile_rows(name))
