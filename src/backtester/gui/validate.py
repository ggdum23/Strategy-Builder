"""Check a run before it starts. Defaults that would change a result must be visible.

Unset borrow, financing, and dividends are passed as ``None`` so the runner
applies the profile's own rule. The transcript says so. Arena rejects a fee
multiple other than 1, a financing rate, and a dividend file; this check
refuses those before the engine is called.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..data import load_dividends
from ..profiles import get_profile
from .preferences import as_path

CONFIG_SCHEMA = "strategy-builder.run_config.v1"

_MODES = {"single", "walkforward", "batch"}
_SOURCES = {"named", "pack"}


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _number(value, label: str, issues: list[str]) -> float | None:
    if _blank(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        issues.append(f"{label} is not a number")
        return None
    if isinstance(value, bool) or number != number or number in (float("inf"), float("-inf")):
        issues.append(f"{label} must be finite")
        return None
    return number


def _optional_int(value, label: str, issues: list[str]) -> int | None:
    if _blank(value):
        return None
    if isinstance(value, bool):
        issues.append(f"{label} must be an integer")
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    issues.append(f"{label} must be an integer")
    return None


def normalize(raw: dict) -> dict:
    """Copy a draft into the dict the worker accepts. Does not invent a cut or a profile."""
    strategies = raw.get("strategies") or ()
    return {
        "mode": str(raw.get("mode") or "").strip().lower(),
        "profile": str(raw.get("profile") or "").strip().lower(),
        "source": str(raw.get("source") or "").strip().lower(),
        "strategies": [str(item) for item in strategies],
        "src": "" if _blank(raw.get("src")) else str(raw.get("src")).strip(),
        "ticker": "" if _blank(raw.get("ticker")) else str(raw.get("ticker")).strip(),
        "pack": "" if _blank(raw.get("pack")) else str(raw.get("pack")).strip(),
        "sidecar": "" if _blank(raw.get("sidecar")) else str(raw.get("sidecar")).strip(),
        "out": "" if _blank(raw.get("out")) else str(raw.get("out")).strip(),
        "bar_minutes": raw.get("bar_minutes", 1),
        "initial_equity": raw.get("initial_equity", 1_000_000),
        "borrow_rate_annual": raw.get("borrow_rate_annual"),
        "financing_rate_annual": raw.get("financing_rate_annual"),
        "fee_mult": raw.get("fee_mult", 1),
        "dividends": "" if _blank(raw.get("dividends")) else str(raw.get("dividends")).strip(),
        "warmup_sessions": raw.get("warmup_sessions", 0),
        "warmup_days": raw.get("warmup_days", 0),
        "design_end_day": raw.get("design_end_day"),
        "design_end_date": "" if _blank(raw.get("design_end_date")) else str(raw.get("design_end_date")).strip(),
        "design_fraction": raw.get("design_fraction"),
        "workers": raw.get("workers", 1),
    }


def validate_config(raw: dict) -> list[str]:
    """Human-readable problems. Empty means the draft can be launched."""
    config = normalize(raw)
    issues: list[str] = []
    if config["mode"] not in _MODES:
        issues.append("choose single, walk-forward, or batch")
    workers = config.get("workers", 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        issues.append("workers must be an integer of at least 1")
    if config["source"] not in _SOURCES:
        issues.append("choose a named folder or a pack")
    profile = None
    if not config["profile"]:
        issues.append("choose a profile on the Strategies page. Arena and Desk are not guessed.")
    else:
        try:
            profile = get_profile(config["profile"])
        except ValueError as exc:
            issues.append(str(exc))
    strategies = config["strategies"]
    if config["mode"] == "batch":
        if len(strategies) < 1:
            issues.append("batch needs at least one strategy file")
    elif len(strategies) != 1:
        issues.append("this mode takes exactly one strategy. Use batch for several.")
    for item in strategies:
        path = as_path(item)
        if not path.is_file():
            issues.append(f"strategy file not found: {item}")
    if not config["out"]:
        issues.append("choose a report directory. None is assumed.")
    out = as_path(config["out"]) if config["out"] else None
    if config["source"] == "named":
        if not config["src"]:
            issues.append("named run needs a filled-file folder")
        elif not as_path(config["src"]).exists():
            issues.append(f"data folder not found: {config['src']}")
        if not config["ticker"]:
            issues.append("named run needs a ticker. It is matched to the file name as typed.")
        if out is not None and config["src"] and as_path(config["src"]).exists():
            src = as_path(config["src"])
            if _inside(out, src) or _inside(src, out):
                issues.append("the report directory must not sit inside the data folder, or the other way around")
    elif config["source"] == "pack":
        if not config["pack"]:
            issues.append("pack run needs the pack directory")
        elif not as_path(config["pack"]).is_dir():
            issues.append(f"pack directory not found: {config['pack']}")
        if not config["sidecar"]:
            issues.append("pack run needs the operator sidecar path. Its contents are not shown.")
        elif not as_path(config["sidecar"]).is_file():
            issues.append(f"sidecar file not found: {config['sidecar']}")
        if config["pack"] and config["sidecar"] and as_path(config["sidecar"]).is_file():
            pack = as_path(config["pack"])
            sidecar = as_path(config["sidecar"])
            if pack == sidecar or _inside(sidecar, pack):
                issues.append("the sidecar must not live inside the pack directory")
        if out is not None and config["pack"] and as_path(config["pack"]).exists() and _inside(out, as_path(config["pack"])):
            issues.append("the report must not live inside the pack directory")
        if config["mode"] == "walkforward":
            issues.append("walk-forward here uses named files. The runner's walk_forward entry takes a frame, not a pack.")
    equity = _number(config["initial_equity"], "initial equity", issues)
    if equity is None:
        issues.append("initial equity is required. The field starts at 1000000, and that number is what gets sent.")
    elif equity <= 0:
        issues.append("initial equity must be positive")
    fee = _number(config["fee_mult"], "fee multiple", issues)
    if fee is None:
        issues.append("fee multiple is required. 1 is the unstressed schedule.")
    elif fee <= 0:
        issues.append("fee multiple must be positive")
    if profile is not None and profile.name == "arena" and fee not in (None, 1.0):
        issues.append("Arena rejects a fee multiple other than 1. The Desk stress switch stays on Desk.")
    financing = _number(config["financing_rate_annual"], "financing rate", issues)
    if profile is not None and profile.name == "arena" and financing not in (None, 0.0):
        issues.append("Arena financing stays unset. The engine rejects a financing rate.")
    borrow = _number(config["borrow_rate_annual"], "borrow rate", issues)
    if borrow is not None and borrow < 0:
        issues.append("borrow rate cannot be negative")
    if config["dividends"]:
        dividend_path = as_path(config["dividends"])
        if not dividend_path.is_file():
            issues.append(f"dividend file not found: {config['dividends']}")
        elif profile is not None and not profile.dividends:
            issues.append("Arena does not take a dividend file.")
        else:
            try:
                load_dividends(dividend_path)
            except (OSError, ValueError) as exc:
                issues.append(f"dividend file: {exc}")
    if config["source"] == "named" and config["mode"] != "walkforward":
        minutes = _optional_int(config["bar_minutes"], "bar minutes", issues)
        if minutes is not None and minutes < 1:
            issues.append("bar minutes must be a positive integer")
    warmup_sessions = _optional_int(config["warmup_sessions"], "warmup sessions", issues)
    if config["mode"] != "walkforward" and warmup_sessions is not None and warmup_sessions < 0:
        issues.append("warmup sessions cannot be negative")
    if config["mode"] == "walkforward":
        warmup_days = _optional_int(config["warmup_days"], "warmup days", issues)
        if warmup_days is not None and warmup_days < 0:
            issues.append("warmup days cannot be negative")
        cuts = [
            not _blank(config["design_end_day"]),
            not _blank(config["design_end_date"]),
            not _blank(config["design_fraction"]),
        ]
        if sum(bool(item) for item in cuts) != 1:
            issues.append("walk-forward needs exactly one of design end day, design end date, or design fraction")
        if not _blank(config["design_end_day"]):
            day = _optional_int(config["design_end_day"], "design end day", issues)
            if day is not None and day < 0:
                issues.append("design end day cannot be negative")
        if not _blank(config["design_fraction"]):
            fraction = _number(config["design_fraction"], "design fraction", issues)
            if fraction is not None and not 0.0 < fraction < 1.0:
                issues.append("design fraction must be strictly between 0 and 1")
    return issues


def api_name(config: dict) -> str:
    config = normalize(config)
    if config["mode"] == "batch":
        return "backtester.runner.run_parallel"
    if config["mode"] == "walkforward":
        return "backtester.runner.walk_forward"
    if config["source"] == "pack":
        return "backtester.runner.run_pack"
    return "backtester.runner.run_named"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_record(raw: dict) -> dict:
    """The full launch record. Unset rates stay null on purpose."""
    config = normalize(raw)
    strategies = []
    for item in config["strategies"]:
        path = as_path(item)
        strategies.append({"path": str(path), "sha256": _sha256(path)})
    arguments = {
        "profile": config["profile"] or None,
        "source": config["source"] or None,
        "src": config["src"] or None,
        "ticker": config["ticker"] or None,
        "pack": config["pack"] or None,
        "sidecar_path": config["sidecar"] or None,
        "out": config["out"] or None,
        "bar_minutes": None if config["source"] == "pack" else config["bar_minutes"],
        "initial_equity": config["initial_equity"],
        "borrow_rate_annual": config["borrow_rate_annual"],
        "financing_rate_annual": config["financing_rate_annual"],
        "fee_mult": config["fee_mult"],
        "dividends": config["dividends"] or None,
        "warmup_sessions": None if config["mode"] == "walkforward" else config["warmup_sessions"],
        "warmup_days": config["warmup_days"] if config["mode"] == "walkforward" else None,
        "design_end_day": config["design_end_day"] if config["mode"] == "walkforward" else None,
        "design_end_date": config["design_end_date"] or None if config["mode"] == "walkforward" else None,
        "design_fraction": config["design_fraction"] if config["mode"] == "walkforward" else None,
        "with_benchmarks": True,
        "share_factor": None,
        "workers": config["workers"] if config["mode"] == "batch" else None,
    }
    notes = [
        "null borrow_rate_annual is passed through. The runner uses the profile default. This window did not substitute a number.",
        "null financing_rate_annual is passed through. Arena stays at 0. Desk uses its own rate.",
        "share_factor null means a named run does not override the loader's 1.0. A pack run reads scale from the sidecar inside the runner.",
        "with_benchmarks is true: the same engine and costs, 1x buy-and-hold, and on Desk also constant 2x.",
        "The sidecar file is named above and is not copied into this record.",
        "workers is the process count a batch passes to run_parallel. It does not change a metric. A single run leaves it null.",
    ]
    return {
        "schema": CONFIG_SCHEMA,
        "api": api_name(config),
        "mode": config["mode"],
        "strategies": strategies,
        "arguments": arguments,
        "notes": notes,
    }


def transcript(raw: dict) -> str:
    """Monospace preview of what will be passed. Includes the problems, if any."""
    record = config_record(raw)
    issues = validate_config(raw)
    lines = [
        f"api: {record['api']}",
        f"mode: {record['mode'] or '(not chosen)'}",
        "strategies:",
    ]
    if record["strategies"]:
        for item in record["strategies"]:
            lines.append(f"  {item['path']}")
    else:
        lines.append("  (none)")
    lines.append("arguments:")
    for key, value in record["arguments"].items():
        if key == "sidecar_path" and value:
            lines.append(f"  {key}: {value}")
            lines.append("  sidecar contents: not displayed")
            continue
        shown = "null  (engine default, not substituted)" if value is None and key.endswith("_annual") else repr(value)
        if key == "share_factor":
            shown = "null  (named files stay 1.0 inside the runner; pack scale stays in the sidecar)"
        lines.append(f"  {key}: {shown}")
    if issues:
        lines.append("blocked:")
        for issue in issues:
            lines.append(f"  - {issue}")
    else:
        lines.append("ready: the arguments above are what the worker will pass")
    return "\n".join(lines)
