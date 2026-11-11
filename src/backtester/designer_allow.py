"""Allow-list for every designer-facing artifact.

``publish_designer`` and the desktop window both import this module. A
designer surface keeps these names and drops every other key. There is no
second designer path that still shows share counts, prices, notionals,
per-component fees, or accounting dollars.

Exact equity stays. Fees are a cost drag in basis points of that equity,
rounded to 0.1 bp, because a dollar fee total divided by the public
per-share schedule recovers the share count. ``docs/PACK.md`` records that
residual.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP

# Returns, ratios, exposure, drawdowns, counts, bar and session indices, and
# the settings that cannot be inverted into a price. No share counts, prices,
# notionals, per-component fees, dollar fee totals, or accounting dollars.
REDACTED_KEYS = frozenset(
    {
        "total_return",
        "cagr",
        "ann_vol",
        "max_drawdown",
        "worst_day",
        "best_day",
        "hit_rate",
        "percent_up_days",
        "excess_total_return",
        "bh_total_return",
        "long_2x_total_return",
        "frac_bars_long",
        "frac_bars_short",
        "frac_sessions_long_at_close",
        "frac_sessions_short_at_close",
        "extended_hours_fill_notional_frac",
        "borrow_rate_annual",
        "financing_rate_annual",
        "cash_rate_annual",
        "withholding_rate",
        "sharpe",
        "sortino",
        "calmar",
        "turnover_per_year",
        "avg_abs_exposure_close",
        "avg_net_exposure_close",
        "avg_abs_exposure_bars",
        "worst_equity_over_hwm",
        "max_gross_leverage",
        "avg_gross_leverage",
        "fee_mult",
        "years",
        "sessions_per_year",
        "equity",
        "exposure",
        "position",
        "initial_equity",
        "final_equity",
        "sessions",
        "n_fills",
        "round_trips",
        "long_round_trips",
        "short_round_trips",
        "max_dd_duration_sessions",
        "margin_calls_queued",
        "margin_calls_filled",
        "avg_holding_sessions",
        "avg_holding_bars",
        "rank",
        "bar_minutes",
        "timeout_s",
        "disqualified",
        "busted",
        "open_position_at_end",
        "anonymized",
        "cancelled",
        "dividends_applied",
        "first_session",
        "last_session",
        "first_day",
        "last_day",
        "session",
        "ordinal",
        "day",
        "minute",
        "bars",
        "start_ordinal",
        "end_ordinal",
        "start_day",
        "end_day",
        "cost_drag_bps",
        "period_returns",
        "monthly",
        "yearly",
        "liquidation",
        "run",
        "benchmarks",
        "buy_and_hold_1x",
        "long_2x",
        "profile",
        "audience",
        "accounting",
        "price_basis",
        "total_return_method",
        "series_kind",
        "accrual",
        "status",
        "error",
        "state",
        "name",
        "strategy_id",
        "source_id",
        "kind",
        "phase",
        "time",
        "side",
        "leg",
        "message",
    }
)

# Engine block labels are zero-padded: Arena ``S01`` and Desk ``B01``, one
# block per 252 sessions. Sixty blocks is about sixty years. The month and
# year stand-ins are the ``M1`` / ``Y1`` labels the window writes when it
# relabels a monthly or yearly map. ``S99`` is outside this schema.
_BLOCK_KEY = re.compile(r"[SB](?:0[1-9]|[1-5][0-9]|60)\Z")
_MONTH_KEY = re.compile(r"M([1-9][0-9]{0,2})\Z")
_YEAR_KEY = re.compile(r"Y([1-9][0-9]{0,1})\Z")
_MONTH_LIMIT = 600
_YEAR_LIMIT = 60


def period_key_allowed(name: str) -> bool:
    """True for an engine block label or a month/year stand-in in the schema."""
    key = str(name)
    if _BLOCK_KEY.fullmatch(key):
        return True
    month = _MONTH_KEY.fullmatch(key)
    if month is not None and int(month.group(1)) <= _MONTH_LIMIT:
        return True
    year = _YEAR_KEY.fullmatch(key)
    return year is not None and int(year.group(1)) <= _YEAR_LIMIT


def redacted_key_allowed(name: str) -> bool:
    """True when a designer surface may keep this field name.

    The screen, the export, and ``publish_designer`` all call this. A key
    that is not listed is dropped.
    """
    key = str(name)
    return key in REDACTED_KEYS or period_key_allowed(key)


def equity_base(metrics: dict):
    """Initial equity when it is a non-zero finite number, otherwise final equity."""
    if not isinstance(metrics, dict):
        return None
    for key in ("initial_equity", "final_equity"):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value != 0:
            return float(value)
    return None


def cost_drag_bps(fees, equity) -> float | None:
    """``fees / equity * 10000``, rounded half-up to 0.1 bp.

    The dollar fee total is not returned. A missing or zero equity leaves
    the drag absent rather than inventing a zero.
    """
    if isinstance(fees, bool) or isinstance(equity, bool):
        return None
    if fees is None or equity is None:
        return None
    try:
        fee = Decimal(str(fees))
        base = Decimal(str(equity))
    except Exception:
        return None
    if not fee.is_finite() or not base.is_finite() or base == 0:
        return None
    bps = (fee / base) * Decimal(10000)
    return float(bps.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def with_cost_drag(value):
    """Copy ``value`` and add ``cost_drag_bps`` wherever ``fees_total`` sits.

    The dollar total stays on the copy so a later allow-list pass can drop
    it. Nested benchmark books get their own drag from their own equity.
    """
    if isinstance(value, list):
        return [with_cost_drag(item) for item in value]
    if not isinstance(value, dict):
        return value
    drag = cost_drag_bps(value.get("fees_total"), equity_base(value))
    copied = {key: with_cost_drag(item) for key, item in value.items()}
    if drag is not None:
        copied["cost_drag_bps"] = drag
    return copied


def project_designer(value):
    """Allow-listed copy of a metrics, fill, trade, liquidation, or benchmark tree.

    Fee dollars become ``cost_drag_bps``. Share counts, prices, notionals,
    per-component fees, and accounting dollars are absent.
    """
    return _keep(with_cost_drag(value))


def _keep(value):
    if isinstance(value, list):
        return [_keep(item) for item in value]
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            name = str(key)
            if not redacted_key_allowed(name):
                continue
            kept[name] = _keep(item)
        return kept
    return value
