"""Report-file contract the window reads. It does not compute these figures.

Phase 3 owns ``metrics.json``, the equity and fill CSVs, the equity PNG, and
``leaderboard.csv``. This module is the shape the window expects. A field or
file that is absent is left absent. Nothing here is filled in with a zero or
with a local recalculation.

Official definitions, from the engine metric functions:

- 252 sessions per year.
- Risk-free rate is 0.
- Returns are regular-session close marks. The first return is against initial equity.
- Sharpe is mean(return) / sample stdev * sqrt(252). Sortino uses the downside
  deviation of the same returns (population form, matching the engines).
- Max drawdown is the minimum of equity / running peak − 1, with initial equity
  on the path. Duration counts sessions from peak until recovery.
- Calmar is CAGR / |max drawdown|.
- Desk daily marks are the last regular-session close of each session.
"""

from __future__ import annotations

from dataclasses import dataclass

CONTRACT = "strategy-builder.report.v1"

ANNUALIZATION = (
    "252 sessions per year. Risk-free rate is 0. "
    "Returns are regular-session close marks, and the first return is measured against initial equity. "
    "The window displays the number stored in the report. It does not recompute it."
)

# A finished book. Everything else is optional and may arrive with Phase 3.
METRICS_FILE = "metrics.json"
WALKFORWARD_FILE = "walkforward.json"
EQUITY_FILES = ("equity_curve.csv", "equity.csv")
FILLS_FILES = ("fills.csv",)
TRADES_FILES = ("trades.csv",)
FIGURE_FILES = ("equity.png", "equity_curve.png")
LIQUIDATION_FILES = ("liquidation.json", "liquidation_postmortem.json")
LEADERBOARD_FILE = "leaderboard.csv"
RUN_CONFIG_FILE = "run_config.json"
BATCH_STATUS_FILE = "batch_status.json"

EQUITY_COLUMNS = ("equity",)
EXPOSURE_COLUMNS = ("exposure", "position")
SESSION_COLUMNS = ("date", "session", "day")

# Operator sidecar keys. A designer-facing view must not render these.
# Named operator runs still show ``cost_basis`` (the invoice, scale 1).
# An anonymized or designer view drops that object. See ``gui.audience``.
WITHHELD_KEYS = frozenset(
    {
        "date_map",
        "price_factor",
        "volume_factor",
        "operator_note",
    }
)

FILL_COLUMNS = frozenset(
    {
        "shares_delta",
        "fill_price",
        "fees",
        "price",
        "shares_after",
    }
)
EPISODE_COLUMNS = frozenset({"pnl", "side", "entry_notional"})

# Keys already presented in a dedicated panel. Other scalar keys are listed
# under "other reported fields" so a Phase 3 addition is visible.
PANEL_KEYS = frozenset(
    {
        "benchmarks",
        "fees_breakdown",
        "period_returns",
        "cost_basis",
        "run",
        "liquidation",
        "profile",
        "symbol",
        "bar_minutes",
        "error",
    }
)


@dataclass(frozen=True)
class MetricSpec:
    """How to label one reported field. ``kind`` is a display format, not a formula."""

    key: str
    label: str
    kind: str
    group: str
    tooltip: str


def _ann(text: str) -> str:
    return f"{text} {ANNUALIZATION}"


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("total_return", "Total return", "fraction", "headline", _ann("Final equity / initial equity − 1.")),
    MetricSpec("cagr", "CAGR", "fraction", "headline", _ann("Compound annual growth from initial equity to final equity.")),
    MetricSpec("ann_vol", "Volatility", "fraction", "headline", _ann("Sample standard deviation of session returns, annualized.")),
    MetricSpec("sharpe", "Sharpe", "ratio", "headline", _ann("Mean session return / sample standard deviation, annualized.")),
    MetricSpec("sortino", "Sortino", "ratio", "headline", _ann("Mean session return / downside deviation, annualized.")),
    MetricSpec("max_drawdown", "Max drawdown", "fraction", "headline", _ann("Minimum of equity / running peak − 1, initial equity included.")),
    MetricSpec(
        "max_dd_duration_sessions",
        "DD duration",
        "count",
        "headline",
        "Longest run of sessions from a peak until equity recovers. Unit: sessions.",
    ),
    MetricSpec("calmar", "Calmar", "ratio", "headline", _ann("CAGR / |max drawdown|. No drawdown is stored as infinity.")),
    MetricSpec(
        "excess_total_return",
        "Excess vs 1×",
        "fraction",
        "kpi",
        "Stored strategy total return minus the 1× book. The window does not subtract the two figures.",
    ),
    MetricSpec("final_equity", "Final equity", "money", "headline", "Account equity at the last session mark. Unit: dollars."),
    MetricSpec("initial_equity", "Initial equity", "money", "activity", "Starting capital recorded by the engine. Unit: dollars."),
    MetricSpec("n_fills", "Fills", "count", "activity", "Number of fills the engine recorded."),
    MetricSpec("fees_total", "Fees", "money", "activity", "Commission and exchange fees recorded by the engine. Slippage is separate. Unit: dollars."),
    MetricSpec("slippage_cost", "Slippage", "money", "activity", "Slippage dollars recorded by the engine. Unit: dollars."),
    MetricSpec("borrow_cost", "Borrow", "money", "activity", "Short-borrow dollars recorded by the engine. Unit: dollars."),
    MetricSpec("costs_total", "Costs", "money", "activity", "Fees, slippage, borrow, and (Desk) financing, as stored. Unit: dollars."),
    MetricSpec("turnover_per_year", "Turnover", "ratio", "activity", _ann("Traded notional / mean equity / years. Unit: times equity per year.")),
    MetricSpec("hit_rate", "Hit rate", "fraction", "activity", "Share of closed round trips with positive pnl. Stored as a fraction."),
    MetricSpec("percent_up_days", "Up sessions", "fraction", "activity", "Share of session marks that rose. Stored as a fraction."),
    MetricSpec("round_trips", "Round trips", "count", "activity", "Closed episodes recorded by the engine."),
    MetricSpec("best_day", "Best session", "fraction", "activity", "Largest session return stored in the report."),
    MetricSpec("worst_day", "Worst session", "fraction", "activity", "Smallest session return stored in the report."),
    MetricSpec("avg_abs_exposure_close", "Avg |exposure|", "ratio", "activity", "Mean absolute exposure at the session mark. Exposure is position value / equity."),
    MetricSpec("avg_net_exposure_close", "Avg net exposure", "ratio", "activity", "Mean signed exposure at the session mark."),
    MetricSpec("sessions", "Sessions", "count", "activity", "Session marks in the equity curve the metrics used."),
    MetricSpec("years", "Years", "year", "activity", "Sessions / 252, as stored. Unit: years."),
    MetricSpec(
        "financing_cost",
        "Financing",
        "money",
        "desk",
        "Desk financing dollars. 7%/252 on max(0, |position value| − equity) unless the run overrode the rate. Unit: dollars.",
    ),
    MetricSpec("dividends_net", "Dividends", "money", "desk", "Net cash dividends the Desk engine credited. Unit: dollars."),
    MetricSpec("margin_calls_queued", "Margin calls queued", "count", "desk", "Times gross leverage at a regular close queued a reduction."),
    MetricSpec("margin_calls_filled", "Margin calls filled", "count", "desk", "Queued margin reductions that filled."),
    MetricSpec("max_gross_leverage", "Max leverage", "leverage", "desk", "Maximum gross leverage the Desk engine stored. Unit: times equity."),
    MetricSpec("avg_gross_leverage", "Avg leverage", "leverage", "desk", "Average gross leverage the Desk engine stored. Unit: times equity."),
    MetricSpec(
        "worst_equity_over_hwm",
        "Worst equity / HWM",
        "ratio",
        "desk",
        "Smallest equity / running high-water mark on any bar. Liquidation is below 0.30. This is the stored ratio, not a drawdown the window derived.",
    ),
    MetricSpec(
        "disqualified",
        "Disqualified",
        "flag",
        "desk",
        "Desk liquidation flag. True means the book was flattened and trading stopped. Displayed only when the report contains the field.",
    ),
    MetricSpec("fee_mult", "Fee multiple", "ratio", "desk", "Desk cost-stress multiple recorded on the run. 1 is the unstressed schedule."),
    MetricSpec("financing_rate_annual", "Financing rate", "fraction", "desk", "Annual financing rate stored on the run. Arena stays at 0."),
    MetricSpec("borrow_rate_annual", "Borrow rate", "fraction", "desk", "Annual borrow rate stored on the run."),
    MetricSpec("share_factor", "Share factor", "ratio", "desk", "Share factor stored on the Desk result. Named raw runs are 1."),
)

SPECS = {spec.key: spec for spec in METRICS}
HEADLINE_KEYS = tuple(spec.key for spec in METRICS if spec.group == "headline")
ACTIVITY_KEYS = tuple(spec.key for spec in METRICS if spec.group == "activity")
DESK_KEYS = tuple(spec.key for spec in METRICS if spec.group == "desk")

# The results page leads with these seven stored fields, in this order.
KPI_KEYS = (
    "total_return",
    "cagr",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "excess_total_return",
)


def leaderboard_kind(key: str) -> str | None:
    """Display kind for one leaderboard column. Ratios are two decimals."""
    spec = SPECS.get(key)
    if spec is None:
        if key in {"rank", "n_fills", "sessions", "round_trips"}:
            return "count"
        return None
    if spec.kind == "ratio":
        return "ratio2"
    return spec.kind

# Columns used when a batch has no leaderboard.csv. Each cell is copied from
# that run's metrics.json. The window does not rank by a formula of its own.
ASSEMBLED_COLUMNS = (
    "strategy",
    "profile",
    "symbol",
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "final_equity",
    "n_fills",
    "fees_total",
    "turnover_per_year",
    "disqualified",
    "financing_cost",
    "borrow_cost",
    "dividends_net",
    "margin_calls_filled",
    "max_gross_leverage",
    "worst_equity_over_hwm",
)

BENCHMARK_TITLES = {
    "buy_and_hold_1x": "1× buy and hold",
    "long_2x": "2× long",
    "buy_and_hold": "1× buy and hold",
    "constant_2x": "2× long",
}
