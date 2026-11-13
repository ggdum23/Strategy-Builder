#!/usr/bin/env python3
"""Time the prepared-data path on a deterministic synthetic series.

``--scale ci`` is 20 sessions, about nineteen thousand one-minute bars.
That is what the perf workflow gates. ``--scale year`` is 252 sessions.
``--scale full`` is 23 years of weekdays. Each scale times a cold load, a
warm load, one Arena book, one Desk book, an Arena walk-forward, the jailed
Arena and Desk runs, and Arena parallel. The ci and year scales use 1, 2, 4,
and 8 workers. The full scale stops at 4 workers.
The generator is synthetic and is not a market download. Nothing it writes
is committed.

The gate limits are loose on purpose. They catch a quadratic loader or a
broken cache. They are not the 23-year target. Report the printed machine
line with the numbers: a different host is not comparable.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCALES = {
    "ci": 20,
    "year": 252,
    "full": 23 * 252,
}

# Seconds. Wide enough that a loaded CI runner still passes, tight enough
# that the old per-session volume loop cannot.
GATES = {
    "cold_load_s": 60.0,
    "warm_load_s": 30.0,
    "arena_s": 60.0,
    "desk_s": 90.0,
    "arena_jailed_s": 180.0,
    "desk_jailed_s": 240.0,
}


class _Hold:
    def on_bar(self, bar, _state):
        phase = bar.get("phase", bar.get("session"))
        if phase == "rth" and bar.get("time") == "09:30":
            return 1.0
        return None


def _rss_mb() -> float:
    try:
        import resource
    except ImportError:
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


def _proc_kb(pid: int, label: str) -> float:
    path = Path(f"/proc/{pid}/smaps_rollup") if label == "Pss" else Path(f"/proc/{pid}/status")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0.0
    prefix = "Pss:" if label == "Pss" else "VmRSS:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.split()[1])
    return 0.0


def _tree_mb() -> tuple[float, float]:
    """RSS and PSS of this process and its descendants, in MiB.

    PSS counts a shared mapping once across the tree. RSS counts it in
    every process. Windows has no ``/proc`` and reports the process high-water
    RSS for both.
    """
    if not Path("/proc/self/status").is_file():
        rss = _rss_mb()
        return rss, rss
    ppid: dict[int, int] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            text = Path(f"/proc/{name}/status").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                ppid[int(name)] = int(line.split()[1])
                break
    members = {os.getpid()}
    changed = True
    while changed:
        changed = False
        for pid, parent in ppid.items():
            if parent in members and pid not in members:
                members.add(pid)
                changed = True
    rss = pss = 0.0
    for pid in members:
        rss += _proc_kb(pid, "VmRSS")
        pss += _proc_kb(pid, "Pss")
    return rss / 1024, pss / 1024


class _Peak:
    """Highest tree RSS and PSS seen while a phase is running."""

    def __init__(self) -> None:
        self.rss = 0.0
        self.pss = 0.0
        self._epoch = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            self._sample()

    def _sample(self) -> None:
        with self._lock:
            epoch = self._epoch
        rss, pss = _tree_mb()
        with self._lock:
            if epoch != self._epoch:
                return
            if rss > self.rss:
                self.rss = rss
            if pss > self.pss:
                self.pss = pss

    def begin(self) -> None:
        gc.collect()
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
        rss, pss = _tree_mb()
        with self._lock:
            if epoch == self._epoch:
                self.rss = rss
                self.pss = pss

    def end(self) -> tuple[float, float]:
        self._sample()
        with self._lock:
            return round(self.rss, 1), round(self.pss, 1)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


def _time(fn):
    start = time.perf_counter()
    value = fn()
    return time.perf_counter() - start, value


def _write_strategies(directory: Path, count: int = 8) -> list[Path]:
    """One strategy file per worker slot so 1/2/4/8 each have real jobs."""
    paths = []
    for index in range(count):
        folder = directory / f"s{index}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "strategy.py"
        decision = (
            "return 1.0 if phase == 'rth' and bar.get('time') == '09:30' else None"
            if index == 0
            else "return None"
        )
        path.write_text(
            "class Strategy:\n"
            "    def on_bar(self, bar, state):\n"
            "        phase = bar.get('phase', bar.get('session'))\n"
            f"        {decision}\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def _write_strategy(directory: Path) -> Path:
    path = directory / "hold.py"
    path.write_text(
        "class Strategy:\n"
        "    def on_bar(self, bar, state):\n"
        "        phase = bar.get('phase', bar.get('session'))\n"
        "        if phase == 'rth' and bar.get('time') == '09:30':\n"
        "            return 1.0\n"
        "        return None\n",
        encoding="utf-8",
    )
    return path


def _jail_available() -> bool:
    if sys.platform == "win32":
        return True
    from shutil import which

    return which("bwrap") is not None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Time prepared-data loads and runs")
    parser.add_argument("--scale", choices=tuple(SCALES), default="ci")
    parser.add_argument("--source", default="", help="Reuse this filled-file directory instead of generating one")
    parser.add_argument("--gate", action="store_true", help="Exit 1 when a ci-scale limit is missed")
    parser.add_argument("--skip-jail", action="store_true")
    parser.add_argument("--skip-parallel", action="store_true")
    parser.add_argument("--skip-walk", action="store_true")
    parser.add_argument("--json", default="", help="Write the measurement JSON here")
    args = parser.parse_args(argv)
    sessions = SCALES[args.scale]
    from backtester.data import load_named, prepare_desk_bars
    from backtester.engine_desk import DeskConfig
    from backtester.synthetic import write_series
    import backtester.engine_arena as engine_arena
    import backtester.engine_desk as engine_desk

    work = Path(tempfile.mkdtemp(prefix="sb-bench-"))
    cache = work / "prepared"
    cache.mkdir()
    os.environ["STRATEGY_BUILDER_PREPARED_DIR"] = str(cache)
    source = Path(args.source) if args.source else work / "src"
    peak = _Peak()
    peak.begin()
    if args.source:
        generate_s = 0.0
    else:
        generate_s, _root = _time(lambda: write_series(source, "SYN", sessions=sessions))
    report_peak = {}
    report_peak["generate"] = peak.end()
    peak.begin()
    cold_s, loaded = _time(lambda: load_named(source, "SYN"))
    rows = int(len(loaded.frame))
    del loaded
    gc.collect()
    report_peak["cold_load"] = peak.end()
    print(f"cold {round(cold_s, 3)}s rss {report_peak['cold_load'][0]} MB pss {report_peak['cold_load'][1]} MB", flush=True)
    peak.begin()
    warm_s, warmed = _time(lambda: load_named(source, "SYN"))
    frame = warmed.frame
    prepared = bool(warmed.prepared)
    del warmed
    report_peak["warm_load"] = peak.end()
    print(f"warm {round(warm_s, 3)}s rows {rows} rss {report_peak['warm_load'][0]} MB pss {report_peak['warm_load'][1]} MB", flush=True)
    peak.begin()
    arena_s, arena = _time(lambda: engine_arena.run(_Hold(), frame, record_fills=False))
    report_peak["arena"] = peak.end()
    print(
        f"arena {round(arena_s, 3)}s equity {engine_arena.metrics(arena)['final_equity']} "
        f"rss {report_peak['arena'][0]} MB pss {report_peak['arena'][1]} MB",
        flush=True,
    )
    peak.begin()
    desk_s, desk = _time(lambda: engine_desk.run(_Hold(), prepare_desk_bars(frame), DeskConfig()))
    report_peak["desk"] = peak.end()
    print(
        f"desk {round(desk_s, 3)}s equity {engine_desk.metrics(desk)['final_equity']} "
        f"rss {report_peak['desk'][0]} MB pss {report_peak['desk'][1]} MB",
        flush=True,
    )
    report = {
        "scale": args.scale,
        "sessions": sessions,
        "rows": rows,
        "machine": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "generate_s": round(generate_s, 3),
        "cold_load_s": round(cold_s, 3),
        "warm_load_s": round(warm_s, 3),
        "arena_s": round(arena_s, 3),
        "desk_s": round(desk_s, 3),
        "arena_equity": engine_arena.metrics(arena)["final_equity"],
        "desk_equity": engine_desk.metrics(desk)["final_equity"],
        "prepared": prepared,
        "rss_mb": report_peak["arena"][0],
        "pss_mb": report_peak["arena"][1],
        "phase_rss_mb": {name: values[0] for name, values in report_peak.items()},
        "phase_pss_mb": {name: values[1] for name, values in report_peak.items()},
    }
    del arena
    del desk
    strategy = _write_strategy(work)
    if not args.skip_walk:
        from backtester.runner import walk_forward

        peak.begin()
        walk_s, _walked = _time(
            lambda: walk_forward(
                strategy,
                frame,
                "arena",
                work / "walk",
                symbol="SYN",
                bar_minutes=1,
                design_fraction=0.5,
                with_benchmarks=False,
            )
        )
        report["walk_forward_arena_s"] = round(walk_s, 3)
        print(f"walk-forward {report['walk_forward_arena_s']}s", flush=True)
        report_peak["walk_forward"] = peak.end()
        report["phase_rss_mb"]["walk_forward"] = report_peak["walk_forward"][0]
        report["phase_pss_mb"]["walk_forward"] = report_peak["walk_forward"][1]
        del _walked
    del frame
    gc.collect()
    from backtester.parallel import NamedMarket, discover_strategies, run_parallel

    if not args.skip_parallel:
        strategies = work / "strategies"
        _write_strategies(strategies)
        market = NamedMarket(str(source), "SYN")
        discovered = discover_strategies(strategies)
        parallel = {}
        parallel_rss = {}
        worker_set = (1, 2, 4) if args.scale == "full" else (1, 2, 4, 8)
        for workers in worker_set:
            peak.begin()
            elapsed, _result = _time(
                lambda workers=workers: run_parallel(
                    discovered,
                    [market],
                    work / f"parallel-{workers}",
                    profile="arena",
                    workers=workers,
                    with_benchmarks=False,
                )
            )
            parallel[str(workers)] = round(elapsed, 3)
            rss, pss = peak.end()
            parallel_rss[str(workers)] = {"rss_mb": rss, "pss_mb": pss}
            gc.collect()
        report["parallel_arena_s"] = parallel
        report["parallel_rss_mb"] = parallel_rss
    if not args.skip_jail and _jail_available():
        from backtester.runner import run_named

        peak.begin()
        out = work / "arena-jail"
        report["arena_jailed_s"] = round(
            _time(
                lambda: run_named(
                    strategy,
                    source,
                    "SYN",
                    "arena",
                    out,
                    with_benchmarks=False,
                    in_process=False,
                )
            )[0],
            3,
        )
        report_peak["arena_jailed"] = peak.end()
        print(f"jailed arena {report['arena_jailed_s']}s rss {report_peak['arena_jailed'][0]} MB pss {report_peak['arena_jailed'][1]} MB", flush=True)
        peak.begin()
        out = work / "desk-jail"
        report["desk_jailed_s"] = round(
            _time(
                lambda: run_named(
                    strategy,
                    source,
                    "SYN",
                    "desk",
                    out,
                    with_benchmarks=False,
                    in_process=False,
                )
            )[0],
            3,
        )
        report_peak["desk_jailed"] = peak.end()
        print(f"jailed desk {report['desk_jailed_s']}s rss {report_peak['desk_jailed'][0]} MB pss {report_peak['desk_jailed'][1]} MB", flush=True)
        report["phase_rss_mb"]["arena_jailed"] = report_peak["arena_jailed"][0]
        report["phase_rss_mb"]["desk_jailed"] = report_peak["desk_jailed"][0]
        report["phase_pss_mb"]["arena_jailed"] = report_peak["arena_jailed"][1]
        report["phase_pss_mb"]["desk_jailed"] = report_peak["desk_jailed"][1]
        report["rss_mb"] = report_peak["arena_jailed"][0]
        report["pss_mb"] = report_peak["arena_jailed"][1]
    peak.close()
    print(
        f"machine {report['machine']}  python {report['python']}  cpus {report['cpus']}"
    )
    print(
        f"scale {args.scale}  sessions {sessions}  rows {report['rows']}  "
        f"generate {report['generate_s']}s"
    )
    print(
        f"load cold {report['cold_load_s']}s  warm {report['warm_load_s']}s  "
        f"arena {report['arena_s']}s  desk {report['desk_s']}s  "
        f"rss {report['rss_mb']} MB  pss {report['pss_mb']} MB"
    )
    if "walk_forward_arena_s" in report:
        print(f"walk-forward arena {report['walk_forward_arena_s']}s")
    if "arena_jailed_s" in report:
        print(
            f"jailed arena {report['arena_jailed_s']}s  desk {report['desk_jailed_s']}s"
        )
    if "parallel_arena_s" in report:
        print(
            "parallel arena "
            + "  ".join(f"w{count} {seconds}s" for count, seconds in report["parallel_arena_s"].items())
        )
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.gate:
        if args.scale != "ci":
            print("the gate applies to --scale ci", file=sys.stderr)
            return 2
        missed = []
        for key, limit in GATES.items():
            if key not in report:
                continue
            if report[key] > limit:
                missed.append(f"{key} {report[key]}s > {limit}s")
        if missed:
            print("gate missed: " + "; ".join(missed), file=sys.stderr)
            return 1
        print("gate ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
