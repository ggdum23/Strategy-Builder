"""Profile rules shown before a run.

Numbers come from ``backtester.profiles``. The sentences say what that profile's
engine does. They are not a second fee schedule.
"""

from __future__ import annotations

from ..profiles import get_profile

_FILL = {
    "next_delivered_bar": (
        "Fills at the open of the next bar the strategy is shown. "
        "With extended hours off, only regular-session bars are delivered, "
        "so the last regular decision fills at the next session's first delivered bar."
    ),
    "next_rth_open": (
        "Fills only at the open of the next regular-session bar, including a later "
        "regular bar of the same session. Pre and post signals queue, and a newer "
        "signal replaces one that has not filled."
    ),
}

_WARMUP = {
    "rescale_keep_exposure": (
        "Warm-up trades, then rescales the account to the starting equity and keeps exposure."
    ),
    "flat_until_live": (
        "Warm-up stays flat. Targets are ignored while live is false, then the book starts flat "
        "at the starting equity."
    ),
}

_FEES = {
    "arena": (
        "Fee table is the Arena schedule in costs.py, including that file's FINRA TAF, "
        "plus 1 bp slippage per side. Financing is 0. Dividends are not credited. "
        "A fee multiple other than 1 is rejected."
    ),
    "desk": (
        "Fee table is the Desk schedule in costs_desk.py. SEC and FINRA TAF are not the Arena "
        "numbers. Slippage is 1 bp per side. fee_mult scales commissions, fees, and slippage. "
        "It does not scale borrow, financing, or the share count that advances the volume tier."
    ),
}


def profile_rows(name: str) -> tuple[tuple[str, str], ...]:
    """Labeled rules for one profile. Unknown names raise, the same as the runner."""
    profile = get_profile(name)
    cap = f"{profile.max_abs_target:g}"
    rows = [
        ("Profile", profile.name),
        ("Clamp", f"Target exposure is clamped to ±{cap}. An unchanged target is a hold: no drift rebalance."),
        ("Fills", _FILL.get(profile.fill_venue, profile.fill_venue)),
        ("Fees", _FEES.get(profile.fee_table, profile.fee_table)),
        (
            "Borrow",
            f"{profile.borrow_rate_annual:.2%} per year on short notional, charged by this profile's engine. "
            "Leave the run field unset to pass that default through. An explicit number overrides it.",
        ),
        (
            "Financing",
            f"{profile.financing_rate_annual:.2%} per year. "
            + (
                "Arena rejects any other rate in this build, so the reference loop stays unchanged."
                if profile.name == "arena"
                else "Desk charges it once per session boundary on max(0, |position value| − equity). Idle cash earns 0."
            ),
        ),
        (
            "Dividends",
            "Not credited." if not profile.dividends else "Cash from the dividend file, before the ex-session's first bar.",
        ),
    ]
    if profile.margin_call_gross is None:
        rows.append(("Margin", "No margin call."))
    else:
        rows.append(
            (
                "Margin",
                f"Gross leverage above {profile.margin_call_gross:g}× at a regular close, with nothing pending, "
                f"queues a fill back to sign(position) × {cap} at the next regular open.",
            )
        )
    if profile.liquidation_hwm_fraction is None:
        rows.append(("Liquidation", "No forced liquidation."))
    else:
        rows.append(
            (
                "Liquidation",
                f"Equity below {profile.liquidation_hwm_fraction:.0%} of the running high-water mark "
                "flattens at that bar's close, stops trading, and flags DISQUALIFIED.",
            )
        )
    rows.append(("Warm-up", _WARMUP.get(profile.warmup, profile.warmup)))
    rows.append(
        (
            "Share count",
            "Named raw files use share factor 1. The runner rejects any other factor. "
            "An anonymized pack keeps its scale in the operator sidecar. The runner reads that file. "
            "This window does not open it and does not offer 0.9 as a choice.",
        )
    )
    rows.append(
        (
            "Benchmarks",
            "1× buy and hold through the same engine and the same costs."
            + (" Desk also runs a constant 2× long, financed at the Desk rate." if profile.name == "desk" else ""),
        )
    )
    return tuple(rows)


def profile_text(name: str) -> str:
    return "\n".join(f"{label}. {body}" for label, body in profile_rows(name))
