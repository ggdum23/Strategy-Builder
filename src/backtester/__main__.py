"""Command line. Printing stays in this module. Walk-forward restores the working directory."""

from __future__ import annotations

import argparse
import secrets
import signal
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

    parallel = sub.add_parser("parallel", help="Score many strategies, each in its own process")
    parallel.add_argument("--strategies", required=True, help="Directory of strategy.py files")
    parallel.add_argument("--workers", required=True, type=int, help="How many strategy processes at once")
    parallel.add_argument("--profile", required=True, choices=("arena", "desk"))
    parallel.add_argument("--out", required=True)
    parallel.add_argument("--ticker", action="append", default=None, help="Repeat for each named symbol")
    parallel.add_argument("--src", action="append", default=None, help="One directory for every ticker, or one per ticker")
    parallel.add_argument("--pack", action="append", default=None)
    parallel.add_argument("--sidecar", action="append", default=None)
    parallel.add_argument("--bar-minutes", type=int, default=1, help="Named files only. A pack keeps the bar size it was built with.")
    parallel.add_argument("--timeout", type=float, default=600.0, help="Seconds for one strategy after its process has started")
    parallel.add_argument("--memory-mb", type=int, default=None, help="Resident-set cap for one strategy process")
    parallel.add_argument("--borrow-rate", type=float, default=None)
    parallel.add_argument("--financing-rate", type=float, default=None)
    parallel.add_argument("--fee-mult", type=float, default=1.0)
    parallel.add_argument("--dividends", default=None, help="One CSV for the whole batch. Desk only.")
    parallel.add_argument("--initial-equity", type=float, default=1_000_000.0)
    parallel.add_argument("--warmup-sessions", type=int, default=0)
    parallel.add_argument("--no-benchmarks", action="store_true")

    report = sub.add_parser("report", help="Read a run by id or by directory")
    report.add_argument("--run", required=True, help="Run id or the directory that contains run.json")
    report.add_argument("--audience", choices=("operator", "designer"), default="operator")

    referee = sub.add_parser("referee", help="One-bar replay (later phase)")
    referee.add_argument("rest", nargs=argparse.REMAINDER)

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
        if args.share_factor is not None:
            raise ValueError(
                "a pack run does not take --share-factor. The sidecar holds the scale."
            )
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
        _remember(
            args.out,
            kind="book",
            profile=payload.get("profile") or args.profile,
            symbol=payload.get("symbol"),
            anonymized=bool((payload.get("run") or {}).get("anonymized")),
        )
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
    _remember(args.out, kind="book", profile=payload.get("profile") or args.profile, symbol=payload.get("symbol"))
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
    _remember(args.out, kind="walkforward", profile=args.profile, symbol=args.ticker)
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
        print("scale factors and the date map are in the sidecar")
    else:
        print("named pack is unscaled (share_scale 1)")
    print(f"pack: {result.pack_dir}")
    print(f"sidecar: {result.sidecar_path}")
    return 0


def _remember(out, *, kind: str, profile: str, symbol: str | None = None, anonymized: bool = False) -> str:
    from .report import register_run

    run_id = "r" + secrets.token_hex(8)
    document = {
        "schema_version": 1,
        "id": run_id,
        "kind": kind,
        "profile": profile,
        "anonymized": bool(anonymized),
    }
    if symbol:
        document["symbol"] = symbol
    write_json(Path(out) / "run.json", document)
    register_run(Path.cwd() / ".backtester-runs.json", run_id, out, kind=kind, profile=profile)
    print(f"run {run_id}")
    return run_id


def _markets(args):
    from .parallel import NamedMarket, PackMarket

    tickers = list(args.ticker or [])
    srcs = list(args.src or [])
    packs = list(args.pack or [])
    sidecars = list(args.sidecar or [])
    if packs or sidecars:
        if tickers or srcs:
            raise ValueError("pass --pack/--sidecar or --ticker/--src, not both")
        if len(packs) != len(sidecars) or not packs:
            raise ValueError("each --pack needs one --sidecar, in the same order")
        if args.bar_minutes != 1:
            raise ValueError("bar size is fixed when the pack is built. Build another pack to change it.")
        return [PackMarket(pack, sidecar) for pack, sidecar in zip(packs, sidecars)]
    if not tickers:
        raise ValueError("parallel needs --ticker or --pack")
    if len(srcs) == 1:
        srcs = srcs * len(tickers)
    if len(srcs) != len(tickers):
        raise ValueError("pass one --src for every ticker, or one --src used for all of them")
    return [
        NamedMarket(src, ticker, bar_minutes=args.bar_minutes)
        for src, ticker in zip(srcs, tickers)
    ]


def _print_progress(event) -> None:
    tail = f" {event.message}" if event.message else ""
    print(f"[{event.completed}/{event.total}] {event.strategy_id} {event.source_id} {event.status}{tail}")


def _cmd_parallel(args) -> int:
    from .parallel import discover_strategies, run_parallel, CancelToken

    token = CancelToken()
    previous = signal.getsignal(signal.SIGINT)

    def _stop(_signum, _frame):
        token.cancel()

    signal.signal(signal.SIGINT, _stop)
    try:
        result = run_parallel(
            discover_strategies(args.strategies),
            _markets(args),
            args.out,
            profile=args.profile,
            workers=args.workers,
            timeout_s=args.timeout,
            memory_bytes=None if args.memory_mb is None else args.memory_mb * 1024 * 1024,
            on_progress=_print_progress,
            cancel=token,
            borrow_rate_annual=args.borrow_rate,
            financing_rate_annual=args.financing_rate,
            fee_mult=args.fee_mult,
            dividends=_dividends(args.dividends),
            warmup_sessions=args.warmup_sessions,
            initial_equity=args.initial_equity,
            with_benchmarks=not args.no_benchmarks,
            index_path=Path.cwd() / ".backtester-runs.json",
        )
    finally:
        signal.signal(signal.SIGINT, previous)
    print(f"run {result.run_id}")
    print(f"leaderboard {result.leaderboard_path}")
    print(f"failures {result.failures}")
    if result.cancelled:
        print("cancelled")
        return 1
    return 1 if result.failures else 0


def _cmd_report(args) -> int:
    from .report import load_report

    view = load_report(args.run, audience=args.audience, index_path=Path.cwd() / ".backtester-runs.json")
    print(
        f"audience={view['audience']} kind={view['kind']} "
        f"id={view.get('id')} profile={view.get('profile')}"
    )
    if view["kind"] == "parallel":
        for row in view["rows"]:
            bits = [
                f"rank={row.get('rank', '')}",
                f"strategy={row.get('strategy_id', '')}",
                f"source={row.get('source_id', '')}",
                f"status={row.get('status', '')}",
                f"total_return={row.get('total_return', '')}",
                f"excess={row.get('excess_total_return', '')}",
            ]
            if args.audience == "operator":
                bits.insert(3, f"symbol={row.get('symbol', '')}")
            print(" ".join(bits))
        if view.get("cancelled"):
            print("cancelled")
        return 0
    if view["kind"] == "walkforward":
        summary = view["summary"]
        for key in ("design_end_date", "locked_start_date", "design_sessions", "locked_sessions", "warmup_rule"):
            if key in summary and not (args.audience == "designer" and key.endswith("_date")):
                print(f"{key}={summary[key]}")
        for label in ("design", "locked"):
            block = summary.get(label) or {}
            if block.get("total_return") is not None:
                print(f"{label} total_return={block['total_return']}")
        return 0
    metrics = view["metrics"]
    for key in (
        "total_return",
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "n_fills",
        "disqualified",
        "financing_cost",
    ):
        if key in metrics and metrics[key] is not None:
            print(f"{key}={metrics[key]}")
    benches = metrics.get("benchmarks") or {}
    for label in ("buy_and_hold_1x", "long_2x"):
        block = benches.get(label) or {}
        if block.get("total_return") is not None:
            print(f"{label} total_return={block['total_return']}")
    if args.audience == "operator" and metrics.get("symbol"):
        print(f"symbol={metrics['symbol']}")
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
            return _cmd_parallel(args)
        if args.command == "referee":
            raise NotImplementedError("The one-bar referee is a later phase.")
        if args.command == "report":
            return _cmd_report(args)
        parser.error(f"unknown command {args.command}")
        return 2
    except (ValueError, FileNotFoundError, NotImplementedError, AttributeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
