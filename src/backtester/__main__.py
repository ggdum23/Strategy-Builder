"""Command line. Printing stays in this module. Walk-forward restores the working directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .data import load_dividends, load_named
from .profiles import ARENA, DESK
from .registry import (
    entry_from_inspection,
    format_list,
    format_show,
    get_series,
    load_or_new,
    load_registry,
    parse_phase_map,
    parse_splits,
    save_registry,
    upsert_series,
)
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


def _add_data_args(parser: argparse.ArgumentParser, *, require_out: bool, require_src: bool = True) -> None:
    parser.add_argument("--strategy", required=True, help="Path to a strategy.py that defines Strategy")
    parser.add_argument("--profile", required=True, choices=("arena", "desk"))
    parser.add_argument("--src", required=require_src, help="Filled-file directory, or one monthly csv.gz")
    parser.add_argument("--ticker", required=require_src)
    parser.add_argument("--bar-minutes", type=int, default=1)
    parser.add_argument("--out", required=require_out)
    parser.add_argument("--share-factor", type=float, default=None, help="Default 1.0 on raw files")
    parser.add_argument(
        "--borrow-rate",
        type=float,
        default=None,
        help=(
            "Annual borrow. Default "
            f"{ARENA.borrow_rate_annual:.4f} on Arena and {DESK.borrow_rate_annual:.4f} on Desk."
        ),
    )
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
    _add_data_args(run, require_out=True, require_src=False)
    run.add_argument("--pack", default=None, help="Chunked pack directory")
    run.add_argument("--sidecar", default=None, help="Operator sidecar for that pack. Never ship this file.")

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

    ingest = sub.add_parser("ingest", help="Inspect filled files and, with --registry, record the series")
    ingest.add_argument("--ticker", required=True)
    ingest.add_argument("--src", required=True)
    ingest.add_argument("--out", default=None, help="Write the inspection JSON here")
    ingest.add_argument("--bar-minutes", type=int, default=1)
    ingest.add_argument("--registry", default=None, help="Registry JSON to create or update")
    ingest.add_argument("--profile", choices=("arena", "desk"), default=None)
    ingest.add_argument("--borrow-rate", type=float, default=None, help="Omit to record the chosen profile's rate")
    ingest.add_argument("--price-kind", choices=("raw", "total_return"), default=None)
    ingest.add_argument("--splits", default=None, help="none, or YYYY-MM-DD:ratio pairs. Recorded, not applied.")
    ingest.add_argument("--phase-map", default=None, help="label=pre,label=rth when the file does not use those names")

    registry = sub.add_parser("registry", help="List or show recorded series")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_list = registry_sub.add_parser("list", help="One line per series")
    registry_list.add_argument("--registry", required=True)
    registry_show = registry_sub.add_parser("show", help="What was inferred for one ticker")
    registry_show.add_argument("--registry", required=True)
    registry_show.add_argument("--ticker", required=True)

    pack = sub.add_parser("pack", help="Write a chunked pack and an operator sidecar")
    pack.add_argument("--ticker", required=True)
    pack.add_argument("--src", required=True)
    pack.add_argument("--out", required=True, help="Empty directory for the designer-facing pack")
    pack.add_argument("--sidecar", required=True, help="Operator file. Must not be inside --out.")
    pack.add_argument("--registry", required=True)
    pack_mode = pack.add_mutually_exclusive_group(required=True)
    pack_mode.add_argument("--anonymize", action="store_true", help="Rescale and hide the calendar")
    pack_mode.add_argument("--named", action="store_true", help="Keep real dates. Share scale stays 1.")
    pack.add_argument("--profile", choices=("arena", "desk"), default=None)
    pack.add_argument("--bar-minutes", type=int, default=1)
    pack.add_argument("--price-factor", type=float, default=None)
    pack.add_argument("--volume-factor", type=float, default=None)
    pack.add_argument("--price-kind", choices=("raw", "total_return"), default="raw")
    pack.add_argument("--phase-map", default=None)

    sub.add_parser("test", help="Run the unit suite")

    for name, help_text in (
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
    basis = payload.get("cost_basis") or {}
    if basis:
        print(
            f"cost basis: share_scale={basis.get('share_scale')} "
            f"schedule={basis.get('cost_schedule')} per_share={basis.get('per_share_fees')}"
        )


def _cmd_run(args) -> int:
    from .runner import run_named, run_pack

    strategy_cls = load_strategy_class(args.strategy)
    if args.pack or args.sidecar:
        if not args.pack or not args.sidecar:
            raise ValueError("a pack run needs both --pack and --sidecar")
        if args.src or args.ticker:
            raise ValueError("a pack run does not take --src or --ticker. The sidecar holds those.")
        if args.bar_minutes != 1:
            raise ValueError("bar size is fixed when the pack is built. Build another pack to change it.")
        payload = run_pack(
            strategy_cls,
            args.pack,
            args.sidecar,
            args.profile,
            args.out,
            borrow_rate_annual=args.borrow_rate,
            financing_rate_annual=args.financing_rate,
            fee_mult=args.fee_mult,
            dividends=_dividends(args.dividends),
            warmup_sessions=args.warmup_sessions,
            initial_equity=args.initial_equity,
        )
        _print_headline(args.profile, payload)
        print(f"wrote {args.out}")
        return 0
    if not args.src or not args.ticker:
        raise ValueError("a named run needs --src and --ticker")
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


def _walk_inputs(args):
    """Load the series and return the frame without keeping the loader object.

    The caller pops ``frame`` in the ``walk_forward`` argument list so this
    command does not keep a local bound to the full series during the design run.
    """
    from .data import load_named as _load

    loaded = _load(args.src, args.ticker, bar_minutes=args.bar_minutes)
    _print_data_notes(loaded.inspection.notes)
    factor = loaded.inspection.share_factor if args.share_factor is None else args.share_factor
    return {
        "frame": loaded.frame,
        "notes": tuple(loaded.inspection.notes),
        "factor": factor,
    }


def _cmd_walk(args) -> int:
    from .runner import walk_forward

    if args.warmup_sessions:
        raise ValueError(
            "walkforward warm-up is --warmup-days, counted back from the locked tail. "
            "--warmup-sessions belongs to `run`."
        )
    strategy_cls = load_strategy_class(args.strategy)
    pack = _walk_inputs(args)
    summary, _slices, _design, _locked = walk_forward(
        strategy_cls,
        pack.pop("frame"),
        args.profile,
        args.out,
        symbol=args.ticker,
        bar_minutes=args.bar_minutes,
        design_end_day=args.design_end_day,
        design_end_date=args.design_end_date,
        design_fraction=args.design_fraction,
        warmup_days=args.warmup_days,
        share_factor=pack["factor"],
        borrow_rate_annual=args.borrow_rate,
        financing_rate_annual=args.financing_rate,
        fee_mult=args.fee_mult,
        dividends=_dividends(args.dividends),
        initial_equity=args.initial_equity,
        inspection_notes=pack["notes"],
    )
    print(
        f"[{args.profile}] design through {summary['design_end_date']} "
        f"({summary['design_sessions']} sessions), locked from {summary['locked_start_date']} "
        f"({summary['locked_sessions']} sessions), warmup_sessions={summary['warmup_sessions']} "
        f"rule={summary['warmup_rule']}"
    )
    print(f"wrote {args.out}")
    return 0


def _print_segments(info) -> None:
    print("  hours inferred from the file:")
    for segment in info.session_segments:
        label = segment.label if segment.engine_phase == segment.label else f"{segment.label}->{segment.engine_phase}"
        print(
            f"    {label} {segment.as_dict()['start_clock']}-{segment.as_dict()['end_clock']} "
            f"({segment.n_sessions} sessions, {segment.modal_bars} bars)"
        )


def _cmd_ingest(args) -> int:
    if args.out is None and args.registry is None:
        raise ValueError("ingest needs --out, --registry, or both")
    if args.registry is not None and args.profile is None:
        raise ValueError("recording a series needs --profile arena or desk. Refusing to guess from the ticker.")
    if args.registry is not None and args.price_kind is None:
        raise ValueError("recording a series needs --price-kind raw or total_return. Refusing to guess.")
    price_kind = args.price_kind or "raw"
    phase_map = parse_phase_map(args.phase_map)
    splits = parse_splits(args.splits)
    loaded = load_named(
        args.src,
        args.ticker,
        bar_minutes=args.bar_minutes,
        phase_map=phase_map,
        price_kind=price_kind,
    )
    info = loaded.inspection
    if args.out is not None:
        payload = info.as_dict()
        if splits:
            payload["declared_splits"] = list(splits)
            payload["runnable"] = False
        write_json(Path(args.out), payload)
    print(
        f"{info.symbol}: {info.native_rows} one-minute bars, {info.n_sessions} sessions, "
        f"{info.first_timestamp_et} .. {info.last_timestamp_et}"
    )
    print(
        f"  prices={info.price_adjustment} share_factor={info.share_factor} "
        f"tick={info.native_tick} fill={info.fill_policy}"
    )
    _print_segments(info)
    _print_data_notes(info.notes)
    if args.out is not None:
        print(f"wrote {args.out}")
    if args.registry is not None:
        borrow = None if args.borrow_rate is None else args.borrow_rate
        entry = entry_from_inspection(
            info,
            source_root=str(args.src),
            profile=args.profile,
            borrow_rate_annual=borrow,
            splits=splits,
        )
        document = upsert_series(load_or_new(args.registry), entry)
        save_registry(args.registry, document)
        state = "runnable" if entry["runnable"] else "recorded, not runnable"
        print(f"registry: {entry['symbol']} {entry['profile']} ({state}) -> {args.registry}")
    return 0


def _cmd_registry(args) -> int:
    document = load_registry(args.registry)
    if args.registry_command == "list":
        print(format_list(document))
        return 0
    if args.registry_command == "show":
        print(format_show(get_series(document, args.ticker)))
        return 0
    raise ValueError(f"unknown registry command {args.registry_command}")


def _cmd_pack(args) -> int:
    from .pack import build_pack

    result = build_pack(
        args.src,
        args.ticker,
        args.out,
        args.sidecar,
        anonymize=bool(args.anonymize),
        profile=args.profile,
        registry_path=args.registry,
        bar_minutes=args.bar_minutes,
        price_factor=args.price_factor,
        volume_factor=args.volume_factor,
        phase_map=parse_phase_map(args.phase_map),
        price_kind=args.price_kind,
    )
    kind = "anonymized" if result.anonymized else "named"
    print(
        f"{kind} pack: {result.n_sessions} sessions in {result.n_chunks} file(s), "
        f"bar_minutes={result.bar_minutes}"
    )
    if result.anonymized:
        print("scale factors and the date map are only in the sidecar")
    else:
        print("named pack is unscaled (share_scale 1)")
    print(f"pack: {result.pack_dir}")
    print(f"sidecar: {result.sidecar_path}")
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
        if args.command == "registry":
            return _cmd_registry(args)
        if args.command == "pack":
            return _cmd_pack(args)
        if args.command == "test":
            return _cmd_test()
        if args.command == "parallel":
            raise NotImplementedError("Parallel scoring is a later phase.")
        if args.command == "referee":
            raise NotImplementedError("The one-bar referee is a later phase.")
        if args.command == "report":
            raise NotImplementedError("A separate report command is a later phase. `run` already writes metrics.json.")
        parser.error(f"unknown command {args.command}")
        return 2
    except (ValueError, FileNotFoundError, NotImplementedError, AttributeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
