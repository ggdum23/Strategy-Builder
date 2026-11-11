"""Read a run directory. Display only.

Every number on the results page comes from a report file. A missing file or a
missing field stays missing. Chart drawdown is the one derived series, and it
is labeled as a picture of the equity column, not as a second max-drawdown.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .audience import apply_designer, book_is_anonymized, peek_scale, peek_symbols, resolve_view
from .format import is_missing
from .schema import (
    ACTIVITY_KEYS,
    ASSEMBLED_COLUMNS,
    BATCH_STATUS_FILE,
    BENCHMARK_TITLES,
    CONTRACT,
    DESK_KEYS,
    EQUITY_COLUMNS,
    EQUITY_FILES,
    EPISODE_COLUMNS,
    EXPOSURE_COLUMNS,
    FIGURE_FILES,
    FILL_COLUMNS,
    FILLS_FILES,
    HEADLINE_KEYS,
    LEADERBOARD_FILE,
    LIQUIDATION_FILES,
    METRICS_FILE,
    PANEL_KEYS,
    RUN_CONFIG_FILE,
    SESSION_COLUMNS,
    SPECS,
    TRADES_FILES,
    WALKFORWARD_FILE,
    WITHHELD_KEYS,
)

_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv"})


@dataclass
class Curve:
    """One equity file, ready to plot. Drawdown is a display transform."""

    name: str
    source: str
    sessions: list[str]
    equity: list[float]
    exposure: list[float] | None
    drawdown: list[float]
    drawdown_note: str


@dataclass
class TableData:
    columns: tuple[str, ...]
    rows: tuple[dict, ...]
    source: str


@dataclass
class RunReport:
    """What the window is allowed to show for one directory."""

    directory: Path
    kind: str
    contract: str = CONTRACT
    metrics: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    curves: list[Curve] = field(default_factory=list)
    fills: TableData | None = None
    episodes: TableData | None = None
    benchmarks: dict = field(default_factory=dict)
    benchmark_curves: list[Curve] = field(default_factory=list)
    cost_basis: dict | None = None
    liquidation: dict | None = None
    liquidation_source: str | None = None
    figure: Path | None = None
    config: dict | None = None
    leaderboard: TableData | None = None
    walkforward: dict | None = None
    children: list[dict] = field(default_factory=list)
    other_fields: dict = field(default_factory=dict)
    withheld: list[str] = field(default_factory=list)
    audience: str = "operator"
    opened: Path | None = None
    redacted: bool = False
    fees_breakdown: dict | None = None
    period_returns: dict | None = None


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _first_file(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _pick(columns, candidates: tuple[str, ...]) -> str | None:
    lookup = {str(column).lower(): column for column in columns}
    for name in candidates:
        if name in lookup:
            return lookup[name]
    return None


def _withhold(payload, withheld: list[str]):
    """Drop operator-only keys if a report ever contains them."""
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            if key in WITHHELD_KEYS:
                if key not in withheld:
                    withheld.append(key)
                continue
            clean[key] = _withhold(value, withheld)
        return clean
    if isinstance(payload, list):
        return [_withhold(item, withheld) for item in payload]
    return payload


def _load_table(path: Path) -> TableData:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        rows = []
        for raw in reader:
            row = {}
            for column in columns:
                row[column] = _coerce(raw.get(column))
            rows.append(row)
    kept = [column for column in columns if column not in WITHHELD_KEYS]
    if len(kept) != len(columns):
        rows = [{key: row.get(key) for key in kept} for row in rows]
    return TableData(columns=tuple(kept), rows=tuple(rows), source=path.name)


def _coerce(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"inf", "+inf", "infinity"}:
        return float("inf")
    if lowered in {"-inf", "-infinity"}:
        return float("-inf")
    if lowered == "nan":
        return float("nan")
    try:
        if any(mark in text for mark in (".", "e", "E")):
            return float(text)
        return int(text)
    except ValueError:
        return text


def _floats(values) -> list[float]:
    out = []
    for value in values:
        if is_missing(value):
            out.append(float("nan"))
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def _drawdown(equity: list[float], initial) -> tuple[list[float], str]:
    """Picture of the equity column. Not the official max drawdown."""
    series = list(equity)
    note = "Drawdown is equity divided by its running peak, minus one."
    if not is_missing(initial):
        try:
            series = [float(initial)] + series
        except (TypeError, ValueError):
            pass
    peak = None
    points = []
    for value in series:
        if value != value:  # NaN
            points.append(float("nan"))
            continue
        if peak is None or value > peak:
            peak = value
        if peak is None or peak <= 0:
            points.append(float("nan"))
        else:
            points.append(value / peak - 1.0)
    if not is_missing(initial) and points:
        points = points[1:]
    return points, note


def _curve(path: Path, name: str, initial) -> Curve | None:
    table = _load_table(path)
    if not table.columns:
        return None
    equity_col = _pick(table.columns, EQUITY_COLUMNS)
    if equity_col is None:
        return None
    session_col = _pick(table.columns, SESSION_COLUMNS)
    exposure_col = _pick(table.columns, EXPOSURE_COLUMNS)
    sessions = []
    equity = []
    exposure = [] if exposure_col else None
    for index, row in enumerate(table.rows):
        sessions.append("" if session_col is None else str(row.get(session_col) or index))
        equity.append(_floats([row.get(equity_col)])[0])
        if exposure is not None:
            exposure.append(_floats([row.get(exposure_col)])[0])
    drawdown, note = _drawdown(equity, initial)
    return Curve(
        name=name,
        source=str(path),
        sessions=sessions,
        equity=equity,
        exposure=exposure,
        drawdown=drawdown,
        drawdown_note=note,
    )


def _align_note(strategy: Curve, other: Curve) -> tuple[list[float], list[float], str]:
    """Place a benchmark on the strategy's session index. No interpolation."""
    if strategy.sessions and other.sessions and any(strategy.sessions) and any(other.sessions):
        lookup = {}
        for index, label in enumerate(other.sessions):
            lookup.setdefault(label, index)
        xs = []
        ys = []
        for index, label in enumerate(strategy.sessions):
            found = lookup.get(label)
            if found is None:
                continue
            xs.append(float(index))
            ys.append(other.equity[found])
        skipped = len(other.equity) - len(ys)
        if skipped > 0:
            note = f"{skipped} benchmark rows had no matching session and were not drawn."
        else:
            note = "Benchmark aligned on the session column."
        return xs, ys, note
    count = min(len(strategy.equity), len(other.equity))
    note = f"Benchmark drawn by row order, {count} rows."
    return [float(i) for i in range(count)], list(other.equity[:count]), note


def _benchmark_dirs(directory: Path) -> list[Path]:
    root = directory / "benchmarks"
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / METRICS_FILE).is_file():
            found.append(child)
    return found


def _compare_summaries(label: str, nested: dict, full: dict, warnings: list[str]) -> None:
    if not isinstance(nested, dict):
        return
    for key, value in nested.items():
        if key not in full or is_missing(value) or is_missing(full.get(key)):
            continue
        if isinstance(value, (int, float)) and isinstance(full.get(key), (int, float)):
            if abs(float(value) - float(full[key])) > 1e-9 + 1e-9 * abs(float(full[key])):
                warnings.append(
                    f"benchmarks/{label}/metrics.json and the parent benchmarks.{key} disagree. "
                    "Showing the benchmark file."
                )
                return


def _other_fields(metrics: dict) -> dict:
    other = {}
    known = set(SPECS) | PANEL_KEYS
    for key, value in metrics.items():
        if key in known or key in WITHHELD_KEYS:
            continue
        if isinstance(value, (dict, list)):
            continue
        other[key] = value
    return other


def _load_book(directory: Path, withheld: list[str]) -> RunReport:
    report = RunReport(directory=directory, kind="book")
    metrics_path = directory / METRICS_FILE
    if not metrics_path.is_file():
        report.kind = "incomplete"
        report.missing.append(METRICS_FILE)
        return report
    try:
        raw = _read_json(metrics_path)
    except (OSError, json.JSONDecodeError) as exc:
        report.kind = "incomplete"
        report.warnings.append(f"{METRICS_FILE} could not be read: {exc}")
        return report
    if not isinstance(raw, dict):
        report.kind = "incomplete"
        report.warnings.append(f"{METRICS_FILE} is not an object")
        return report
    metrics = _withhold(raw, withheld)
    report.metrics = metrics
    report.other_fields = _other_fields(metrics)
    fees = metrics.get("fees_breakdown")
    if isinstance(fees, dict):
        report.fees_breakdown = fees
    periods = metrics.get("period_returns")
    if isinstance(periods, dict):
        report.period_returns = periods
    basis = metrics.get("cost_basis")
    if isinstance(basis, dict):
        report.cost_basis = basis
    initial = metrics.get("initial_equity")
    equity_path = _first_file(directory, EQUITY_FILES)
    if equity_path is None:
        report.missing.append(" or ".join(EQUITY_FILES))
    else:
        curve = _curve(equity_path, "Strategy", initial)
        if curve is None:
            report.warnings.append(f"{equity_path.name} has no equity column")
        else:
            report.curves.append(curve)
    fills_path = _first_file(directory, FILLS_FILES)
    if fills_path is None:
        report.missing.append(FILLS_FILES[0])
    else:
        table = _load_table(fills_path)
        if FILL_COLUMNS & set(table.columns) or not (EPISODE_COLUMNS & set(table.columns)):
            report.fills = table
        else:
            report.episodes = table
            report.missing.append(FILLS_FILES[0])
    trades_path = _first_file(directory, TRADES_FILES)
    if trades_path is not None and (report.fills is None or trades_path != fills_path):
        table = _load_table(trades_path)
        if report.episodes is None:
            report.episodes = table
    figure = _first_file(directory, FIGURE_FILES)
    report.figure = figure
    if figure is None:
        report.missing.append(FIGURE_FILES[0])
    liquidation = metrics.get("liquidation") if isinstance(metrics.get("liquidation"), dict) else None
    extra = _first_file(directory, LIQUIDATION_FILES)
    if extra is not None:
        try:
            document = _withhold(_read_json(extra), withheld)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"{extra.name} could not be read: {exc}")
            document = None
        if isinstance(document, dict):
            report.liquidation = document
            report.liquidation_source = extra.name
    elif liquidation is not None:
        report.liquidation = liquidation
        report.liquidation_source = f"{METRICS_FILE} liquidation"
    elif "liquidation" not in metrics and _looks_like_desk(metrics):
        report.missing.append("liquidation")
    nested = metrics.get("benchmarks") if isinstance(metrics.get("benchmarks"), dict) else {}
    seen = set()
    for folder in _benchmark_dirs(directory):
        seen.add(folder.name)
        try:
            full = _withhold(_read_json(folder / METRICS_FILE), withheld)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"benchmarks/{folder.name}/{METRICS_FILE} could not be read: {exc}")
            continue
        if isinstance(full, dict):
            report.benchmarks[folder.name] = full
            _compare_summaries(folder.name, nested.get(folder.name), full, report.warnings)
            path = _first_file(folder, EQUITY_FILES)
            if path is not None:
                curve = _curve(path, BENCHMARK_TITLES.get(folder.name, folder.name), full.get("initial_equity", initial))
                if curve is not None:
                    report.benchmark_curves.append(curve)
    for label, summary in nested.items():
        if label in seen or label in WITHHELD_KEYS:
            continue
        if isinstance(summary, dict):
            report.benchmarks[label] = summary
            report.warnings.append(
                f"benchmarks.{label} is only the summary inside {METRICS_FILE}. No benchmarks/{label}/ folder was found."
            )
    if not report.benchmarks:
        report.missing.append("benchmarks")
    config_path = directory / RUN_CONFIG_FILE
    if config_path.is_file():
        try:
            document = _read_json(config_path)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"{RUN_CONFIG_FILE} could not be read: {exc}")
        else:
            if isinstance(document, dict):
                report.config = _withhold(document, withheld)
    return report


def _looks_like_desk(metrics: dict) -> bool:
    if metrics.get("profile") == "desk":
        return True
    return any(key in metrics for key in ("disqualified", "financing_cost", "margin_calls_filled"))


def _load_walkforward(directory: Path, withheld: list[str]) -> RunReport:
    report = RunReport(directory=directory, kind="walkforward")
    try:
        summary = _withhold(_read_json(directory / WALKFORWARD_FILE), withheld)
    except (OSError, json.JSONDecodeError) as exc:
        report.kind = "incomplete"
        report.warnings.append(f"{WALKFORWARD_FILE} could not be read: {exc}")
        return report
    if not isinstance(summary, dict):
        report.kind = "incomplete"
        report.warnings.append(f"{WALKFORWARD_FILE} is not an object")
        return report
    report.walkforward = summary
    report.metrics = summary
    for leg in ("design", "locked"):
        leg_dir = directory / leg
        if (leg_dir / METRICS_FILE).is_file():
            book = _load_book(leg_dir, withheld)
            report.children.append({"leg": leg, "report": book})
        else:
            report.missing.append(f"{leg}/{METRICS_FILE}")
    config_path = directory / RUN_CONFIG_FILE
    if config_path.is_file():
        try:
            document = _read_json(config_path)
            if isinstance(document, dict):
                report.config = _withhold(document, withheld)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"{RUN_CONFIG_FILE} could not be read: {exc}")
    return report


def _child_dirs(directory: Path) -> list[Path]:
    found = []
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_dir() or child.name in _SKIP_DIRS or child.name == "benchmarks":
            continue
        if (child / METRICS_FILE).is_file() or (child / WALKFORWARD_FILE).is_file() or (child / "error.txt").is_file():
            found.append(child)
    return found


def _assembled_leaderboard(directory: Path, children: list[Path]) -> TableData:
    rows = []
    for child in children:
        metrics_path = child / METRICS_FILE
        metrics = {}
        if metrics_path.is_file():
            try:
                loaded = _read_json(metrics_path)
                if isinstance(loaded, dict):
                    metrics = loaded
            except (OSError, json.JSONDecodeError):
                metrics = {}
        config = {}
        config_path = child / RUN_CONFIG_FILE
        if config_path.is_file():
            try:
                loaded = _read_json(config_path)
                if isinstance(loaded, dict):
                    config = loaded
            except (OSError, json.JSONDecodeError):
                config = {}
        strategy = ""
        listed = config.get("strategies") if isinstance(config.get("strategies"), list) else []
        if listed and isinstance(listed[0], dict):
            strategy = str(listed[0].get("path") or "")
        row = {"strategy": strategy or child.name}
        for column in ASSEMBLED_COLUMNS:
            if column == "strategy":
                continue
            if column in metrics:
                row[column] = metrics.get(column)
            else:
                row[column] = None
        error_path = child / "error.txt"
        if error_path.is_file() and not metrics:
            row["strategy"] = strategy or child.name
        rows.append(row)
    return TableData(
        columns=ASSEMBLED_COLUMNS,
        rows=tuple(rows),
        source=f"assembled from each run's {METRICS_FILE} under {directory.name} (no metric was recomputed)",
    )


def _load_batch(directory: Path, withheld: list[str]) -> RunReport:
    report = RunReport(directory=directory, kind="batch")
    children = _child_dirs(directory)
    status_path = directory / BATCH_STATUS_FILE
    failures = {}
    if status_path.is_file():
        try:
            status = _read_json(status_path)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"{BATCH_STATUS_FILE} could not be read: {exc}")
            status = None
        if isinstance(status, dict):
            for row in status.get("strategies") or []:
                if isinstance(row, dict) and row.get("state") == "failed":
                    failures[str(row.get("out") or "")] = str(row.get("error") or "failed")
    board_path = directory / LEADERBOARD_FILE
    if board_path.is_file():
        table = _load_table(board_path)
        report.leaderboard = TableData(table.columns, table.rows, board_path.name)
    elif children:
        report.leaderboard = _assembled_leaderboard(directory, children)
        report.missing.append(LEADERBOARD_FILE)
    else:
        report.missing.append(LEADERBOARD_FILE)
    for child in children:
        entry = {"path": str(child), "name": child.name, "state": "ok", "error": None}
        if not (child / METRICS_FILE).is_file():
            entry["state"] = "failed" if (child / "error.txt").is_file() else "incomplete"
            error_path = child / "error.txt"
            if error_path.is_file():
                entry["error"] = error_path.read_text(encoding="utf-8").strip()
            elif str(child) in failures:
                entry["error"] = failures[str(child)]
        report.children.append(entry)
    config_path = directory / RUN_CONFIG_FILE
    if config_path.is_file():
        try:
            document = _read_json(config_path)
            if isinstance(document, dict):
                report.config = _withhold(document, withheld)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"{RUN_CONFIG_FILE} could not be read: {exc}")
    return report


def _nested_job_dirs(folder: Path) -> list[tuple[str, str, Path]]:
    """``jobs/<strategy>/<source>/`` or a designer bundle ``<strategy>/<source>/``."""
    found = []
    try:
        strategies = sorted(path for path in folder.iterdir() if path.is_dir() and path.name not in _SKIP_DIRS)
    except OSError:
        return found
    for strategy in strategies:
        if strategy.name == "benchmarks":
            continue
        try:
            sources = sorted(path for path in strategy.iterdir() if path.is_dir())
        except OSError:
            continue
        for source in sources:
            if source.name in {"benchmarks", "designer"}:
                continue
            if (
                (source / METRICS_FILE).is_file()
                or (source / "error.json").is_file()
                or (source / "designer" / "error.json").is_file()
            ):
                found.append((strategy.name, source.name, source))
    return found


def _is_parallel_tree(folder: Path) -> bool:
    document = None
    info = folder / "run.json"
    if info.is_file():
        try:
            loaded = _read_json(info)
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            document = loaded
    if isinstance(document, dict) and document.get("kind") == "parallel":
        return True
    jobs = folder / "jobs"
    return jobs.is_dir() and bool(_nested_job_dirs(jobs))


def _job_error(directory: Path) -> str | None:
    for relative in ("error.json", Path("designer") / "error.json"):
        path = directory / relative
        if not path.is_file():
            continue
        try:
            document = _read_json(path)
        except (OSError, json.JSONDecodeError):
            text = path.read_text(encoding="utf-8").strip()
            return text or None
        if isinstance(document, dict):
            text = document.get("error")
            if isinstance(text, str) and text.strip():
                return text.strip()
            return json.dumps(document)
    legacy = directory / "error.txt"
    if legacy.is_file():
        return legacy.read_text(encoding="utf-8").strip() or None
    return None


def _load_parallel(directory: Path, withheld: list[str]) -> RunReport:
    """Phase 3 tree: ``jobs/<id>/<source>/`` plus ``designer/`` and ``error.json``."""
    report = RunReport(directory=directory, kind="batch")
    jobs_root = directory / "jobs"
    pairs = _nested_job_dirs(jobs_root) if jobs_root.is_dir() else []
    if not pairs:
        pairs = _nested_job_dirs(directory)
    for strategy_id, source_id, job in pairs:
        metrics_path = job / METRICS_FILE
        error = _job_error(job)
        if metrics_path.is_file():
            state = "ok"
        elif error:
            state = "failed"
        else:
            state = "incomplete"
        report.children.append(
            {
                "path": str(job),
                "name": f"{strategy_id}/{source_id}",
                "strategy_id": strategy_id,
                "source_id": source_id,
                "state": state,
                "error": None if state == "ok" else error,
            }
        )
    board_path = directory / LEADERBOARD_FILE
    if board_path.is_file():
        table = _load_table(board_path)
        report.leaderboard = TableData(table.columns, table.rows, board_path.name)
    elif pairs:
        report.missing.append(LEADERBOARD_FILE)
    else:
        report.missing.append(LEADERBOARD_FILE)
        report.warnings.append("No jobs/ tree and no leaderboard.csv in this batch.")
    info = directory / "run.json"
    if info.is_file():
        try:
            document = _read_json(info)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"run.json could not be read: {exc}")
        else:
            if isinstance(document, dict) and not report.children:
                for job in document.get("jobs") or []:
                    if not isinstance(job, dict):
                        continue
                    report.children.append(
                        {
                            "path": "",
                            "name": f"{job.get('strategy_id')}/{job.get('source_id')}",
                            "strategy_id": job.get("strategy_id"),
                            "source_id": job.get("source_id"),
                            "state": "ok" if job.get("status") == "ok" else str(job.get("status") or "failed"),
                            "error": None,
                        }
                    )
    config_path = directory / RUN_CONFIG_FILE
    if config_path.is_file():
        try:
            document = _read_json(config_path)
            if isinstance(document, dict):
                report.config = _withhold(document, withheld)
        except (OSError, json.JSONDecodeError) as exc:
            report.warnings.append(f"{RUN_CONFIG_FILE} could not be read: {exc}")
    return report


def _dispatch(folder: Path, withheld: list[str]) -> RunReport:
    if _is_parallel_tree(folder) or (folder / LEADERBOARD_FILE).is_file() and _nested_job_dirs(folder):
        return _load_parallel(folder, withheld)
    if (folder / WALKFORWARD_FILE).is_file():
        return _load_walkforward(folder, withheld)
    if (folder / METRICS_FILE).is_file():
        return _load_book(folder, withheld)
    if (folder / BATCH_STATUS_FILE).is_file() or (folder / LEADERBOARD_FILE).is_file() or _child_dirs(folder):
        return _load_batch(folder, withheld)
    report = RunReport(directory=folder, kind="incomplete")
    report.missing.append(METRICS_FILE)
    return report


def load_run(directory, *, audience: str = "auto") -> RunReport:
    """Classify ``directory`` and read the report files that audience may see.

    ``auto`` opens an anonymized book, or any folder that published ``designer/``,
    as a designer view. Pass ``operator`` to read the operator files on purpose.
    """
    folder = Path(directory)
    if not folder.is_dir():
        report = RunReport(directory=folder, kind="incomplete", audience="operator", opened=folder)
        report.warnings.append(f"report directory not found: {folder}")
        return report
    chosen, root, redacted = resolve_view(folder, audience)
    withheld: list[str] = []
    report = _dispatch(root, withheld)
    report.audience = chosen
    report.opened = folder
    report.redacted = redacted
    report.withheld = withheld
    if chosen == "designer":
        apply_designer(report, peek_symbols(folder), redacted=redacted, scale=peek_scale(folder))
    elif withheld:
        report.warnings.append(
            "Withheld operator fields that were present in a report file: " + ", ".join(withheld) + "."
        )
    return report


def chart_overlay(report: RunReport) -> tuple[list[tuple[str, list[float], list[float]]], str]:
    """Strategy equity plus benchmark equity, aligned for the picture only."""
    if not report.curves:
        return [], "No equity file in this run."
    strategy = report.curves[0]
    xs = [float(index) for index in range(len(strategy.equity))]
    lines = [(strategy.name, xs, strategy.equity)]
    notes = []
    for other in report.benchmark_curves:
        ox, oy, note = _align_note(strategy, other)
        lines.append((other.name, ox, oy))
        notes.append(note)
    return lines, " ".join(notes)


def exposure_line(report: RunReport) -> tuple[list[float], list[float]] | None:
    if not report.curves or report.curves[0].exposure is None:
        return None
    curve = report.curves[0]
    return [float(index) for index in range(len(curve.exposure))], list(curve.exposure)


def drawdown_line(report: RunReport) -> tuple[list[float], list[float], str] | None:
    if not report.curves:
        return None
    curve = report.curves[0]
    return [float(index) for index in range(len(curve.drawdown))], list(curve.drawdown), curve.drawdown_note


def present_keys(metrics: dict, keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(key for key in keys if key in metrics)


def headline_keys(metrics: dict) -> tuple[str, ...]:
    return HEADLINE_KEYS


def activity_keys(metrics: dict) -> tuple[str, ...]:
    return tuple(key for key in ACTIVITY_KEYS if key in metrics)


def desk_keys(metrics: dict) -> tuple[str, ...]:
    if not _looks_like_desk(metrics) and not any(key in metrics for key in DESK_KEYS):
        return ()
    return DESK_KEYS


@dataclass(frozen=True)
class HistoryRow:
    path: str
    kind: str
    profile: str | None
    symbol: str | None
    strategy: str | None
    modified: str
    total_return: float | None
    final_equity: float | None
    sharpe: float | None
    max_drawdown: float | None
    disqualified: bool | None


def _history_hides_symbol(metrics: dict) -> bool:
    """An anonymized book does not contribute its ticker to the history list."""
    return book_is_anonymized(metrics)


def _history_from_metrics(folder: Path, kind: str) -> HistoryRow:
    metrics = {}
    metrics_path = folder / METRICS_FILE
    if kind == "walkforward":
        metrics_path = folder / "locked" / METRICS_FILE
        if not metrics_path.is_file():
            metrics_path = folder / WALKFORWARD_FILE
    if metrics_path.is_file():
        try:
            loaded = _read_json(metrics_path)
            if isinstance(loaded, dict):
                metrics = loaded
        except (OSError, json.JSONDecodeError):
            metrics = {}
    config = {}
    config_path = folder / RUN_CONFIG_FILE
    if config_path.is_file():
        try:
            loaded = _read_json(config_path)
            if isinstance(loaded, dict):
                config = loaded
        except (OSError, json.JSONDecodeError):
            config = {}
    strategy = None
    listed = config.get("strategies") if isinstance(config.get("strategies"), list) else []
    if listed and isinstance(listed[0], dict):
        strategy = listed[0].get("path")
    stamp = metrics_path if metrics_path.is_file() else folder
    try:
        modified = stamp.stat().st_mtime
        from datetime import datetime

        modified_text = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
    except OSError:
        modified_text = ""
    flag = metrics.get("disqualified") if "disqualified" in metrics else None
    if isinstance(flag, str):
        flag = flag.lower() == "true"
    arguments = config.get("arguments") if isinstance(config.get("arguments"), dict) else {}

    def _num(key):
        value = metrics.get(key)
        if is_missing(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return HistoryRow(
        path=str(folder),
        kind=kind,
        profile=metrics.get("profile") or arguments.get("profile"),
        symbol=None if _history_hides_symbol(metrics) else (metrics.get("symbol") or arguments.get("ticker")),
        strategy=None if strategy is None else str(strategy),
        modified=modified_text,
        total_return=_num("total_return"),
        final_equity=_num("final_equity"),
        sharpe=_num("sharpe"),
        max_drawdown=_num("max_drawdown"),
        disqualified=flag if isinstance(flag, bool) else None,
    )


def scan_history(root, limit: int = 500) -> list[HistoryRow]:
    """Find run directories. Does not open market-data files."""
    folder = Path(root)
    if not folder.is_dir():
        return []
    rows: list[HistoryRow] = []

    def consider(path: Path) -> None:
        if len(rows) >= limit:
            return
        if (path / WALKFORWARD_FILE).is_file():
            rows.append(_history_from_metrics(path, "walkforward"))
            return
        if (path / METRICS_FILE).is_file():
            parent = path.parent
            if (parent / WALKFORWARD_FILE).is_file() and path.name in {"design", "locked"}:
                return
            rows.append(_history_from_metrics(path, "book"))

    consider(folder)
    for dirpath, dirnames, filenames in os_walk(folder):
        if len(rows) >= limit:
            break
        current = Path(dirpath)
        depth = len(current.relative_to(folder).parts)
        if depth > 4:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.endswith(".csv.gz")]
        if current != folder:
            if WALKFORWARD_FILE in filenames:
                consider(current)
                dirnames[:] = [name for name in dirnames if name not in {"design", "locked", "benchmarks"}]
                continue
            if METRICS_FILE in filenames:
                consider(current)
                dirnames[:] = [name for name in dirnames if name != "benchmarks"]
    return rows


def os_walk(root: Path):
    import os

    return os.walk(root)


def read_designer_manifest(pack_dir) -> dict:
    """Manifest fields a designer is allowed to see. The sidecar is not opened."""
    path = Path(pack_dir) / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("manifest.json is not an object")
    allowed = (
        "schema_version",
        "anonymized",
        "bar_minutes",
        "asset_name",
        "pack_id",
        "columns",
        "chunks",
        "daily_file",
        "n_sessions",
    )
    manifest = {key: document[key] for key in allowed if key in document}
    for key in WITHHELD_KEYS:
        if key in document:
            raise ValueError(f"manifest.json contains {key}, which does not belong in a designer-facing pack")
    return manifest
