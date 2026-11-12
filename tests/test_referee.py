"""One-bar referee: engine parity, profile warm-up, and the OS jail."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from backtester.__main__ import main
from backtester.referee import (
    Disqualified,
    SandboxError,
    prepare_book,
    probe,
    replay,
    run_in_process,
    run_referee,
)
from backtester.runner import execute
from backtester.sandbox import describe_isolation, launch
from backtester.strategy_loader import load_strategy_class
from windows_open import OWNER_PC

ROOT = Path(__file__).resolve().parents[1]
SELFTEST = ROOT / "examples" / "selftest"
SCALE = "1.1375918462"


def _dates(count: int, start: str) -> list[str]:
    day = datetime.fromisoformat(start)
    found = []
    while len(found) < count:
        if day.weekday() < 5:
            found.append(day.date().isoformat())
        day += timedelta(days=1)
    return found


def _frame(dates, minutes=(570, 571), price=100.0) -> pd.DataFrame:
    rows = []
    for index, date in enumerate(dates):
        for minute in minutes:
            px = float(price)
            rows.append(
                {
                    "day": index,
                    "date": date,
                    "weekday": index % 5,
                    "minute": minute,
                    "time": f"{minute // 60:02d}:{minute % 60:02d}",
                    "session": "rth",
                    "open": px,
                    "high": px + 0.5,
                    "low": px - 0.5,
                    "close": px,
                    "volume": 1000.0,
                    "wap": px,
                    "count": 10,
                    "filled": 0,
                    "short_session": 0,
                }
            )
    return pd.DataFrame(rows)


def _write(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


def _load(path: Path):
    return load_strategy_class(path)()


HOLD = """
class Strategy:
    def __init__(self):
        self.sent = False

    def on_bar(self, bar, state):
        phase = bar.get("phase", bar.get("session"))
        minute = bar.get("minute")
        if minute is None and bar.get("time") == "09:30":
            minute = 570
        if self.sent or phase != "rth" or int(minute) != 570:
            return None
        self.sent = True
        return 1.0
"""

ONCE = """
class Strategy:
    def on_bar(self, bar, state):
        if int(bar.get("day", -1)) == 0 and int(bar.get("minute", -1)) == 570:
            return 1.0
        return None
"""

ROUND = """
class Strategy:
    def on_bar(self, bar, state):
        minute = bar.get("minute")
        if minute is None:
            return None
        minute = int(minute)
        if minute == 570:
            return 1.0
        if minute == 571:
            return 0.0
        return None
"""

ALWAYS = """
class Strategy:
    def on_bar(self, bar, state):
        phase = bar.get("phase", bar.get("session"))
        if phase == "rth":
            return 1.0
        return None
"""


def _needs_jail():
    if sys.platform not in {"linux", "win32"}:
        pytest.skip("the referee jail is Linux or Windows")


def _assert_same_book(left, right):
    assert left["fills"] == right["fills"]
    assert left["daily"] == right["daily"]
    assert left["fees"]["total"] == right["fees"]["total"]
    assert left["n_fills"] == right["n_fills"]


def test_bare_referee_command_exits_2(capsys):
    assert main(["referee"]) == 2
    captured = capsys.readouterr()
    assert "referee needs" in captured.err
    assert "--strategy" in captured.err


def test_desk_rejects_a_session_count_warmup():
    with pytest.raises(ValueError, match="Arena rule"):
        prepare_book(_frame(_dates(2, "2024-01-02")), profile="desk", warmup_days=504)
    assert main(
        [
            "referee",
            "--strategy",
            "strategy.py",
            "--hidden",
            "hidden",
            "--profile",
            "desk",
            "--out",
            "out",
            "--warmup-days",
            "10",
        ]
    ) == 2


def test_pack_and_named_share_factor_are_refused(tmp_path, capsys):
    named = main(
        [
            "referee",
            "--strategy",
            "strategy.py",
            "--hidden",
            str(tmp_path / "named"),
            "--profile",
            "arena",
            "--ticker",
            "QQQ",
            "--share-factor",
            "0.9",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert named == 2
    assert "share_factor" in capsys.readouterr().err

    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "side.json").write_text("{}", encoding="utf-8")
    refused = main(
        [
            "referee",
            "--strategy",
            "strategy.py",
            "--hidden",
            str(pack),
            "--sidecar",
            str(tmp_path / "side.json"),
            "--profile",
            "arena",
            "--share-factor",
            "0.9",
            "--out",
            str(tmp_path / "pack-out"),
        ]
    )
    assert refused == 2
    assert "share-factor" in capsys.readouterr().err


def test_arena_warmup_keeps_exposure_and_desk_stays_flat():
    train = _frame(_dates(6, "2024-01-02"), minutes=(570,), price=100.0)
    hidden = _frame(_dates(2, "2024-01-10"), minutes=(570,), price=100.0)
    arena = prepare_book(hidden, profile="arena", train=train, warmup_days=4)
    assert arena.warmup_rule == "rescale_keep_exposure"
    assert arena.reset_day == 4
    assert arena.warmup_sessions == 4
    result, _metrics = run_in_process(_BuyDay0(), arena)
    assert result["daily"][0][1] == 1_000_000.0
    assert result["daily"][0][3] != 0
    assert result["fills"] == []

    desk = prepare_book(hidden, profile="desk", train=train)
    assert desk.warmup_rule == "flat_until_live"
    assert desk.live_start == 6
    desk_result, _desk_metrics = run_in_process(_AlwaysLong(), desk)
    train_dates = set(_dates(6, "2024-01-02"))
    assert desk_result["n_fills"] >= 1
    assert train_dates.isdisjoint({row["session"] for row in desk_result["fills"]})


class _BuyDay0:
    WANTS_EXTENDED_HOURS = False

    def on_bar(self, bar, state):
        del state
        if int(bar.get("day", -1)) == 0 and int(bar.get("minute", -1)) == 570:
            return 1.0
        return None


class _AlwaysLong:
    WANTS_EXTENDED_HOURS = False

    def on_bar(self, bar, state):
        del state
        phase = bar.get("phase", bar.get("session"))
        if phase == "rth":
            return 1.0
        return None


def test_default_arena_warmup_is_504_sessions_and_not_the_desk_rule():
    train = _frame(_dates(600, "2018-01-02"), minutes=(570,), price=50.0)
    hidden = _frame(_dates(1, "2021-06-01"), minutes=(570,), price=50.0)
    prepared = prepare_book(hidden, profile="arena", train=train)
    assert prepared.reset_day == 504
    assert prepared.warmup_sessions == 504
    assert prepared.warmup_rule == "rescale_keep_exposure"
    desk = prepare_book(hidden, profile="desk", train=train)
    assert desk.live_start == 600
    assert desk.warmup_rule == "flat_until_live"
    assert desk.reset_day is None


def test_colliding_pack_session_ids_stay_distinct_and_keep_the_calendar():
    train = _frame(["2024-01-02", "2024-01-03"], minutes=(570,))
    train["_calendar"] = train["date"]
    train["date"] = ["S00001", "S00001", "S00002", "S00002"][: len(train)]
    # two minutes, so each date is repeated. Assign by the calendar.
    train["date"] = ["S00001" if cal == "2024-01-02" else "S00002" for cal in train["_calendar"]]
    hidden = _frame(["2024-02-01"], minutes=(570,))
    hidden["_calendar"] = hidden["date"]
    hidden["date"] = "S00001"
    prepared = prepare_book(hidden, profile="desk", train=train, warmup_days=None)
    dates = list(dict.fromkeys(prepared.frame["date"].tolist()))
    assert dates == ["S00001", "S00002", "S00003"]
    assert prepared.month_of_session("S00001") == 202401
    assert prepared.month_of_session("S00003") == 202402
    assert "2024-01-02" not in set(prepared.frame["date"].tolist())
    with pytest.raises(ValueError, match="overlap"):
        same = _frame(["2024-01-02"], minutes=(570,))
        prepare_book(same, profile="arena", train=same, warmup_days=1)


def test_calendar_month_changes_the_fee_and_matches_the_engine(tmp_path):
    dates = _dates(22, "2024-01-02")
    frame = _frame(dates, price=10.0)
    prepared = prepare_book(frame, profile="arena")
    path = _write(tmp_path / "round.py", ROUND)
    strategy = _load(path)
    calendar, _metrics = run_in_process(strategy, prepared)
    blocks, _block_metrics = execute(_load(path), prepared.frame, "arena")
    assert calendar["fees"]["total"] != blocks["fees"]["total"]
    _needs_jail()
    remote, _remote_metrics, backend, _guarantees = replay(path, prepared, call_timeout=5)
    assert backend
    _assert_same_book(remote, calendar)


def test_short_sample_matches_the_normal_runner(tmp_path):
    _needs_jail()
    frame = _frame(_dates(3, "2024-03-04"), price=100.0)
    prepared = prepare_book(frame, profile="arena")
    path = _write(tmp_path / "hold.py", HOLD)
    remote, remote_metrics, _backend, _guarantees = replay(path, prepared, call_timeout=5)
    local, local_metrics = run_in_process(_load(path), prepared)
    official, official_metrics = execute(_load(path), prepared.frame, "arena")
    _assert_same_book(remote, local)
    _assert_same_book(remote, official)
    assert remote_metrics["final_equity"] == official_metrics["final_equity"]
    assert remote_metrics["n_fills"] == local_metrics["n_fills"]

    desk_frame = _frame(_dates(3, "2024-03-04"), price=80.0)
    desk = prepare_book(desk_frame, profile="desk")
    desk_path = _write(tmp_path / "desk_hold.py", HOLD)
    desk_remote, _desk_metrics, _backend, _guarantees = replay(desk_path, desk, call_timeout=5)
    desk_local, _desk_local_metrics = run_in_process(_load(desk_path), desk)
    _assert_same_book(desk_remote, desk_local)
    desk_official, _desk_official_metrics = execute(_load(desk_path), desk.frame, "desk")
    assert desk_remote["fees"]["total"] == desk_official["fees"]["total"]
    assert [row["equity"] for row in desk_remote["daily"]] == [row["equity"] for row in desk_official["daily"]]


def test_warmup_replay_matches_the_in_process_engine(tmp_path):
    _needs_jail()
    train = _frame(_dates(4, "2024-04-01"), minutes=(570,), price=100.0)
    hidden = _frame(_dates(2, "2024-04-08"), minutes=(570,), price=100.0)
    arena = prepare_book(hidden, profile="arena", train=train, warmup_days=4)
    path = _write(tmp_path / "once.py", ONCE)
    remote, _metrics, _backend, _guarantees = replay(path, arena, call_timeout=5)
    local, _local_metrics = run_in_process(_load(path), arena)
    _assert_same_book(remote, local)
    assert remote["daily"][0][1] == 1_000_000.0
    assert remote["daily"][0][3] != 0

    desk = prepare_book(hidden, profile="desk", train=train)
    always = _write(tmp_path / "always.py", ALWAYS)
    desk_remote, _desk_metrics, _backend, _guarantees = replay(always, desk, call_timeout=5)
    desk_local, _desk_local_metrics = run_in_process(_load(always), desk)
    _assert_same_book(desk_remote, desk_local)
    assert desk.live_start == 4
    assert set(_dates(4, "2024-04-01")).isdisjoint({row["session"] for row in desk_remote["fills"]})


def test_share_scale_bills_in_the_parent_and_is_not_visible(tmp_path):
    _needs_jail()
    repo = str(ROOT)
    source = f"""
import json
import os
import sys
from pathlib import Path

TOKEN = {SCALE!r}
REPO = {repo!r}

def _leaked(bar, state):
    blob = json.dumps({{"bar": bar, "state": state}}, default=str)
    text = blob + "\\n" + " ".join(sys.argv)
    text += "\\n" + "\\n".join(f"{{key}}={{value}}" for key, value in os.environ.items())
    cmdline = Path("/proc/1/cmdline")
    if cmdline.is_file():
        text += "\\n" + cmdline.read_bytes().replace(b"\\x00", b" ").decode("utf-8", "replace")
    for root in list(sys.path):
        base = Path(root)
        if not base.is_dir():
            continue
        files = list(base.glob("*.pth"))
        files.extend(base.glob("*.egg-link"))
        files.extend(base.glob("*.dist-info/direct_url.json"))
        for path in files:
            try:
                text += "\\n" + path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    if "share_scale" in text or "share_factor" in text:
        return True
    if TOKEN in text or REPO in text:
        return True
    return False

class Strategy:
    def __init__(self):
        self.sent = False

    def on_bar(self, bar, state):
        if _leaked(bar, state):
            return 0.5
        phase = bar.get("phase", bar.get("session"))
        minute = bar.get("minute")
        if minute is None and bar.get("time") == "09:30":
            minute = 570
        if self.sent or phase != "rth" or int(minute) != 570:
            return None
        self.sent = True
        return 1.0
"""
    path = _write(tmp_path / "scale.py", source)
    frame = _frame(_dates(3, "2024-05-01"), price=50.0)
    prepared = prepare_book(frame, profile="arena")
    remote, _metrics, _backend, _guarantees = replay(path, prepared, call_timeout=5, share_scale=float(SCALE))
    honest, _honest_metrics = run_in_process(_load(_write(tmp_path / "honest.py", HOLD)), prepared, share_scale=float(SCALE))
    plain, _plain_metrics = run_in_process(_load(_write(tmp_path / "plain.py", HOLD)), prepared, share_scale=1.0)
    _assert_same_book(remote, honest)
    assert remote["fees"]["total"] != plain["fees"]["total"]


@pytest.mark.parametrize(
    "name,expect",
    [
        pytest.param("net.py", 0.0, marks=OWNER_PC),
        ("fork.py", 0.0),
        ("setsid.py", 0.0),
        ("parentmem.py", 0.0),
        ("readfile.py", 0.0),
        ("arm.py", 0.0),
    ],
)
def test_selftest_probes_are_blocked(name, expect):
    _needs_jail()
    if name == "setsid.py" and sys.platform != "linux":
        pytest.skip("setsid is the Linux escape")
    server = None
    if name == "net.py":
        # The probe connects to a live listener and to a public address.
        # ECONNREFUSED from a closed port is not evidence of a block.
        import socket

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 44123))
        server.listen(1)
        server.settimeout(0.2)
    try:
        assert probe(SELFTEST / name, call_timeout=8, startup_timeout=20) == expect
    finally:
        if server is not None:
            server.close()
        leftover = ROOT / ".referee-arm-test"
        if leftover.exists():
            leftover.unlink()


def test_hidden_file_and_arm_file_stay_on_the_host(tmp_path):
    _needs_jail()
    secret = tmp_path / "secret.txt"
    secret.write_text("SB-HIDDEN-MARKER", encoding="utf-8")
    marker = ROOT / ".referee-arm-test"
    if marker.exists():
        marker.unlink()
    source = f"""
from pathlib import Path

class Strategy:
    def on_bar(self, bar, state):
        try:
            if "SB-HIDDEN-MARKER" in Path({str(secret)!r}).read_text(encoding="utf-8"):
                return 1.0
        except OSError:
            pass
        target = Path({str(marker)!r})
        try:
            target.write_text("armed", encoding="utf-8")
            if target.read_text(encoding="utf-8") == "armed":
                return 1.0
        except OSError:
            pass
        return 0.0
"""
    path = _write(tmp_path / "files.py", source)
    try:
        assert probe(path, deny_paths=[tmp_path, ROOT], call_timeout=5, startup_timeout=20) == 0.0
        assert secret.read_text(encoding="utf-8") == "SB-HIDDEN-MARKER"
        assert not marker.exists()
    finally:
        if marker.exists():
            marker.unlink()


def test_a_crash_and_a_protocol_break_are_disqualified(tmp_path):
    _needs_jail()
    with pytest.raises(Disqualified) as crashed:
        probe(SELFTEST / "crash.py", call_timeout=5, startup_timeout=20)
    assert crashed.value.reason == "strategy"
    assert "crash" in crashed.value.detail

    extra = _write(
        tmp_path / "extra.py",
        """
import sys

class Strategy:
    def on_bar(self, bar, state):
        sys.stdout.write('{"op":"decision","target":1}\\n{"op":"decision","target":2}\\n')
        sys.stdout.flush()
        return 0.0
""",
    )
    with pytest.raises(Disqualified) as protocol:
        probe(extra, call_timeout=5, startup_timeout=20)
    assert protocol.value.reason == "protocol"


@OWNER_PC
def test_sleep_and_lookahead_are_disqualified_within_eight_seconds():
    """The call budget is 1s. The wall clock includes startup and must stay under 8s.

    On Windows this bound was 13.1s while each launch paid for ``icacls``.
    The grant is once per machine now. The bound stays strict on Linux.
    """
    _needs_jail()
    started = time.monotonic()
    with pytest.raises(Disqualified) as slept:
        probe(SELFTEST / "sleep.py", call_timeout=1, startup_timeout=20)
    assert slept.value.reason == "timeout"
    assert time.monotonic() - started < 8

    started = time.monotonic()
    with pytest.raises(Disqualified) as ahead:
        probe(SELFTEST / "lookahead.py", call_timeout=1, startup_timeout=20)
    assert ahead.value.reason == "timeout"
    assert time.monotonic() - started < 8


def test_memory_cap_stops_the_allocation():
    _needs_jail()
    try:
        target = probe(
            SELFTEST / "mem.py",
            memory_bytes=256 * 1024 * 1024,
            call_timeout=20,
            startup_timeout=20,
        )
    except Disqualified as exc:
        assert exc.reason in {"crash", "strategy", "timeout"}
    else:
        assert target == 0.0


def test_child_does_not_outlive_the_referee():
    _needs_jail()
    box = launch(
        SELFTEST / "sleep.py",
        memory_bytes=256 * 1024 * 1024,
        cpu_seconds=30,
        deny_paths=[ROOT],
    )
    pid = box.process.pid
    try:
        time.sleep(0.2)
        if sys.platform != "win32":
            assert Path(f"/proc/{pid}").exists()
    finally:
        box.close()
    assert box.process.poll() is not None
    if sys.platform != "win32":
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
            time.sleep(0.05)
        assert not Path(f"/proc/{pid}").exists()


def test_launch_fails_closed_without_a_jail(tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Windows fails closed inside the job setup, which this host cannot skip")
    import backtester.sandbox as sandbox

    monkeypatch.setattr(sandbox, "_bwrap_available", lambda: False)
    monkeypatch.setattr(sandbox, "_landlock_available", lambda: False)
    path = _write(tmp_path / "flat.py", "class Strategy:\n    def on_bar(self, bar, state):\n        return 0.0\n")
    with pytest.raises(SandboxError, match="refusing"):
        sandbox.launch(path, memory_bytes=128 * 1024 * 1024, cpu_seconds=5, deny_paths=[])
    with pytest.raises(SandboxError, match="refusing"):
        launch(path, memory_bytes=128 * 1024 * 1024, cpu_seconds=5, deny_paths=["/usr"])


def _probe_bubblewrap_and_landlock(path, monkeypatch, **kwargs) -> None:
    settings = {"call_timeout": 8, "startup_timeout": 20}
    settings.update(kwargs)
    assert probe(path, **settings) == 0.0
    import backtester.sandbox as sandbox

    if not sandbox._landlock_available():
        pytest.skip("landlock is not available")
    monkeypatch.setattr(sandbox, "_bwrap_available", lambda: False)
    assert probe(path, **settings) == 0.0


def test_io_uring_setup_is_denied_on_both_backends(monkeypatch):
    if sys.platform != "linux":
        pytest.skip("io_uring is the Linux seccomp gap")
    _needs_jail()
    _probe_bubblewrap_and_landlock(SELFTEST / "iouring.py", monkeypatch)


def test_a_spawn_loop_is_capped_on_both_backends(monkeypatch):
    if sys.platform != "linux":
        pytest.skip("RLIMIT_NPROC is the Linux process ceiling")
    _needs_jail()
    # The loop clones this interpreter. The address-space cap has to hold
    # those copies, or the loop stops on ENOMEM before the process ceiling.
    _probe_bubblewrap_and_landlock(
        SELFTEST / "nproc.py",
        monkeypatch,
        memory_bytes=8 * 1024 * 1024 * 1024,
        call_timeout=30,
    )


def test_landlock_blocks_the_data_file_when_bubblewrap_is_hidden(tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("landlock is the Linux fallback")
    import backtester.sandbox as sandbox

    if not sandbox._landlock_available():
        pytest.skip("landlock is not available")
    monkeypatch.setattr(sandbox, "_bwrap_available", lambda: False)
    secret = tmp_path / "secret.txt"
    secret.write_text("SB-LANDLOCK-MARKER", encoding="utf-8")
    path = _write(
        tmp_path / "read.py",
        f"""
from pathlib import Path

class Strategy:
    def on_bar(self, bar, state):
        try:
            text = Path({str(secret)!r}).read_text(encoding="utf-8")
        except OSError:
            return 0.0
        return 1.0 if "SB-LANDLOCK-MARKER" in text else 0.0
""",
    )
    assert probe(path, deny_paths=[tmp_path], call_timeout=5, startup_timeout=20) == 0.0


def test_windows_environment_block_is_sorted_and_terminated(monkeypatch):
    from backtester.sandbox import (
        SandboxError,
        _is_volume_root,
        _windows_env,
        _windows_env_block,
        _windows_env_entries,
    )

    entries = _windows_env_entries({"TEMP": "t", "path": "p", "COMSPEC": "c"})
    assert entries == ["COMSPEC=c", "path=p", "TEMP=t"]
    block = _windows_env_block({"TEMP": "t", "PATH": "p"})
    text = "".join(block)
    assert text == "PATH=p\0TEMP=t\0\0"
    anchor = Path(Path.cwd().anchor)
    assert _is_volume_root(anchor)
    assert not _is_volume_root(Path.cwd())
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(SandboxError, match="LOCALAPPDATA"):
        _windows_env(Path("work"))
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")
    env = _windows_env(Path("work"))
    assert env["LOCALAPPDATA"] == "C:\\Users\\x\\AppData\\Local"
    assert env["TEMP"] == str(Path("work"))
    assert env["USERPROFILE"] == str(Path("work"))


def test_describe_isolation_matches_the_host():
    info = describe_isolation()
    assert info["fail_closed"] is True
    if sys.platform == "linux":
        assert info["backend"] in {"bubblewrap", "landlock+seccomp"}
    elif sys.platform == "win32":
        assert info["backend"] == "job+appcontainer"
        assert "KILL_ON_JOB_CLOSE" in info["process"]
        assert "active process limit 1" in info["process"]


def test_cli_replay_matches_the_runner_and_writes_referee_json(tmp_path):
    _needs_jail()
    src = _named_sample(tmp_path / "src")
    strategy = _write(tmp_path / "strategy.py", HOLD)
    out = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtester",
            "referee",
            "--strategy",
            str(strategy),
            "--hidden",
            str(src),
            "--profile",
            "arena",
            "--ticker",
            "NIK",
            "--out",
            str(out),
            "--no-benchmarks",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "final_equity=" in proc.stdout
    assert "share_scale" not in proc.stdout or "share_scale=1" in proc.stdout
    document = json.loads((out / "referee.json").read_text(encoding="utf-8"))
    assert document["disqualified"] is False
    assert document["fail_closed"] is True
    assert document["call_timeout_s"] == 10.0
    assert document["warmup"] == "none"
    metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    from backtester.data import load_named

    loaded = load_named(src, "NIK")
    official, official_metrics = execute(_load(strategy), loaded.frame, "arena")
    assert metrics["final_equity"] == official_metrics["final_equity"]
    assert metrics["n_fills"] == official["n_fills"]
    assert (out / "equity_curve.csv").is_file()
    assert (out / "run.json").is_file()
    assert "NIK" not in (out / "referee.json").read_text(encoding="utf-8")


def test_cli_crash_exits_1(tmp_path):
    _needs_jail()
    src = _named_sample(tmp_path / "src")
    out = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtester",
            "referee",
            "--strategy",
            str(SELFTEST / "crash.py"),
            "--hidden",
            str(src),
            "--profile",
            "desk",
            "--ticker",
            "NIK",
            "--out",
            str(out),
            "--no-benchmarks",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stderr
    assert "disqualified" in proc.stderr
    error = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert error["status"] == "disqualified"
    assert error["reason"] == "strategy"
    referee = json.loads((out / "referee.json").read_text(encoding="utf-8"))
    assert referee["disqualified"] is True


def _named_sample(root: Path) -> Path:
    import gzip
    from datetime import timezone

    et = timezone(timedelta(hours=-4))
    header = [
        "timestamp_et",
        "epoch_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "wap",
        "count",
        "filled",
        "session",
    ]
    lines = [",".join(header)]
    day = datetime(2024, 3, 4)
    for _session in range(2):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        for minute, price in ((570, 100.0), (571, 101.0)):
            stamp = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
            stamp = stamp.replace(tzinfo=et)
            row = {
                "timestamp_et": stamp.isoformat(),
                "epoch_utc": int(stamp.timestamp()),
                "open": price,
                "high": price + 0.4,
                "low": price - 0.4,
                "close": price + 0.1,
                "volume": 1000,
                "wap": price,
                "count": 4,
                "filled": 0,
                "session": "rth",
            }
            lines.append(",".join(str(row[name]) for name in header))
        day += timedelta(days=1)
    path = root / "NIK_1min_filled_2024-03.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return root
