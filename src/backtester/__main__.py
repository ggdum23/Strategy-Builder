"""Command line. Library calls do not print and do not read the working directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .data import load_dividends, load_named
from .report import write_json
from .strategy_loader import load_strategy_class


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "tests").is_dir():
            return parent
    raise FileNotFoundError(
        "python -m backtester test needs a source checkout that contains tests/ and pyproject.toml"
    )


def _add_data_args(parser: argparse.ArgumentParser, *, require_out: bool) -> None:
    parser.add_argument("--strategy", required=True, help="Path to a strategy.py that defines Strategy")
    parser.add_argument("--profile", required=True, choices=("arena", "desk"))
    parser.add_argument("--src", required=True, help="Filled-file directory, or one monthly csv.gz")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--bar-minutes", type=int, default=1)
    parser.add_argument("--out", required=require_out)
    parser.add_argument("--share-factor", type=float, default=None, help="Default 1.0 on raw files")
    parser.add_argument("--borrow-rate", type=float, default=None, help="Annual borrow. Default is the profile rate.")
    parser.add_argument("--financing-rate", type=float, default=None, help="Desk only. Default 0.07. Arena stays at 0.")
    parser.add_argument("--fee-mult", type=float, default=1.0, help="Desk only. Doubles fees and slippage when set to 2.")
    parser.add_argument("--dividends", default=None, help="CSV with columns session,amount. Desk only.")
    parser.add_argument("--initial-equity", type=float, default=1_000_000.0)
    parser.add_argument(
        "--warmup-sessions",
        type=int,
        default=0,
        help="Arena: trade these sessions, then rescale and keep exposure. Desk: flat, live=false.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backtester", description="Local 1-minute strategy backtester")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one strategy and the profile benchmarks")
    _add_data_args(run, require_out=True)

    walk = sub.add_parser("walkforward", help="Design window, then a locked tail")
    _add_data_args(walk, require_out=True)
    walk.add_argument("--design-end-day", type=int, default=None)
    walk.add_argument("--design-end-date", default=None)
    walk.add_argument("--design-fraction", type=float, default=None)
    walk.add_argument(
        "--warmup-days",
        type=int,
        default=0,
        help="Sessions of history before the locked tail. Profile decides what warm-up does.",
    )

    ingest = sub.add_parser("ingest", help="Inspect filled files in place and write a JSON description")
    ingest.add_argument("--ticker", required=True)
    ingest.add_argument("--src", required=True)
    ingest.add_argument("--out", required=True)
    ingest.add_argument("--bar-minutes", type=int, default=1)

    sub.add_parser("test", help="Run the unit suite")

    for name, help_text in (
        ("pack", "Anonymized pack builder (later phase)"),
        ("parallel", "Process-pool scoring (later phase)"),
        ("referee", "One-bar replay (later phase)"),
        ("report", "Report viewer (later phase)"),
    ):
        stub = sub.add_parser(name, help=help_text)
        stub.add_argument("rest", nargs=argparse.REMAINDER)

    return parser


def _dividends(path):
    if path is None:
        return None
    return load_dividends(path)


def _print_data_notes(notes) -> None:
    for note in notes or ():
        if str(note).startswith("Missing monthly file"):
            print(f"note: {note}")


def _print_headline(profile: str, payload: dict) -> None:
    final = payload.get("final_equity")
    total = payload.get("total_return")
    sharpe = payload.get("sharpe")
    fills = payload.get("n_fills")
    print(
        f"[{profile}] final_equity={final:.2f} total_return={total:.6f} sharpe={sharpe:.4f} n_fills={fills}"
    )
    benchmarks = payload.get("benchmarks") or {}
    for label, metrics in benchmarks.items():
        if not metrics or metrics.get("final_equity") is None:
            continue
        print(
            f"[{profile}] {label:16s} final_equity={metrics['final_equity']:.2f} "
            f"total_return={metrics['total_return']:.6f}"
        )
    for warning in (payload.get("run") or {}).get("warnings") or []:
        print(f"warning: {warning}")


def _cmd_run(args) -> int:
    from .runner import run_named

    strategy_cls = load_strategy_class(args.strategy)
    payload = run_named(
        strategy_cls,
        args.src,
        args.ticker,
        args.profile,
        args.out,
        bar_minutes=args.bar_minutes,
        share_factor=args.share_factor,
        borrow_rate_annual=args.borrow_rate,
        financing_rate_annual=args.financing_rate,
        fee_mult=args.fee_mult,
        dividends=_dividends(args.dividends),
        warmup_sessions=args.warmup_sessions,
        initial_equity=args.initial_equity,
    )
    _print_headline(args.profile, payload)
    _print_data_notes(((payload.get("run") or {}).get("inspection") or {}).get("notes"))
    print(f"wrote {args.out}")
    return 0


def _cmd_walk(args) -> int:
    from .runner import walk_forward

    if args.warmup_sessions:
        raise ValueError(
            "walkforward warm-up is --warmup-days, counted back from the locked tail. "
            "--warmup-sessions belongs to `run`."
        )
    strategy_cls = load_strategy_class(args.strategy)
    loaded_factor = args.share_factor
    from .data import load_named as _load

    loaded = _load(args.src, args.ticker, bar_minutes=args.bar_minutes)
    _print_data_notes(loaded.inspection.notes)
    factor = loaded.inspection.share_factor if loaded_factor is None else loaded_factor
    summary, _slices, _design, _locked = walk_forward(
        strategy_cls,
        loaded.frame,
        args.profile,
        args.out,
        symbol=args.ticker,
        bar_minutes=args.bar_minutes,
        design_end_day=args.design_end_day,
        design_end_date=args.design_end_date,
        design_fraction=args.design_fraction,
        warmup_days=args.warmup_days,
        share_factor=factor,
        borrow_rate_annual=args.borrow_rate,
        financing_rate_annual=args.financing_rate,
        fee_mult=args.fee_mult,
        dividends=_dividends(args.dividends),
        initial_equity=args.initial_equity,
    )
    print(
        f"[{args.profile}] design through {summary['design_end_date']} "
        f"({summary['design_sessions']} sessions), locked from {summary['locked_start_date']} "
        f"({summary['locked_sessions']} sessions), warmup_sessions={summary['warmup_sessions']} "
        f"rule={summary['warmup_rule']}"
    )
    print(f"wrote {args.out}")
    return 0


def _cmd_ingest(args) -> int:
    loaded = load_named(args.src, args.ticker, bar_minutes=args.bar_minutes)
    write_json(Path(args.out), loaded.inspection.as_dict())
    info = loaded.inspection
    print(
        f"{info.symbol}: {info.n_rows} bars, {info.n_sessions} sessions, "
        f"{info.first_timestamp_et} .. {info.last_timestamp_et}, "
        f"sessions={','.join(info.session_labels)}, share_factor={info.share_factor}, "
        f"prices={info.price_adjustment}"
    )
    _print_data_notes(info.notes)
    print(f"wrote {args.out}")
    return 0


def _cmd_test() -> int:
    import pytest

    root = _repo_root()
    return int(pytest.main(["-q", str(root / "tests")]))


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "walkforward":
            return _cmd_walk(args)
        if args.command == "ingest":
            return _cmd_ingest(args)
        if args.command == "test":
            return _cmd_test()
        if args.command == "pack":
            raise NotImplementedError("Pack building is a later phase. Named runs do not anonymize.")
        if args.command == "parallel":
            raise NotImplementedError("Parallel scoring is a later phase.")
        if args.command == "referee":
            raise NotImplementedError("The one-bar referee is a later phase.")
        if args.command == "report":
            raise NotImplementedError("A separate report command is a later phase. `run` already writes metrics.json.")
        parser.error(f"unknown command {args.command}")
        return 2
    except (ValueError, FileNotFoundError, NotImplementedError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
