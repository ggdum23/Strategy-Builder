from __future__ import annotations

import hashlib
import re
from pathlib import Path

from backtester import comment_guard

ROOT = Path(__file__).resolve().parents[1]
PINNED_COSTS_SHA256 = "a3b0a522d8bf469001e75aaf274b0c5d3cbef624a68ed9fb94d29298110fe281"
PINNED_COSTS_DESK_SHA256 = "07b0d25ceeebe292f03a2b7b6ca0d76d76fa8d6a59f21d60103abfceeab0ebbd"


def test_comment_file_allowlist_is_exactly_the_two_pinned_cost_files():
    assert comment_guard.FILE_ALLOWLIST == (
        "src/backtester/costs.py",
        "src/backtester/costs_desk.py",
    )
    assert comment_guard.DIRECTIVE_ALLOWLIST == ()


def test_repository_has_no_comments_outside_the_pinned_cost_files():
    findings = comment_guard.scan(ROOT)
    assert findings == [], comment_guard.failure_message(findings)


def test_failure_message_names_the_only_files_allowed_to_contain_comments():
    message = comment_guard.failure_message([comment_guard.Finding("a.py", 3, "comment")])
    assert message.startswith("comments are permitted only in costs.py and costs_desk.py")
    assert "a.py:3: comment" in message


def test_pinned_cost_files_keep_their_bytes_and_the_vendored_copy_is_comment_free():
    costs = ROOT / "src" / "backtester" / "costs.py"
    desk = ROOT / "src" / "backtester" / "costs_desk.py"
    vendored = ROOT / "tests" / "reference" / "arena" / "costs.py"
    assert hashlib.sha256(costs.read_bytes()).hexdigest() == PINNED_COSTS_SHA256
    assert hashlib.sha256(desk.read_bytes()).hexdigest() == PINNED_COSTS_DESK_SHA256
    assert comment_guard.strip_python(costs.read_text(encoding="utf-8")) == vendored.read_text(encoding="utf-8")
    assert comment_guard.python_issue_lines(vendored.read_text(encoding="utf-8")) == []
    assert comment_guard.python_issue_lines(costs.read_text(encoding="utf-8"))


def test_python_scanner_flags_comments_and_docstrings_and_keeps_string_hashes():
    assert comment_guard.python_issue_lines("x = 1  # note\n") == [(1, "comment")]
    assert comment_guard.python_issue_lines('"""module"""\n') == [(1, "docstring")]
    assert comment_guard.python_issue_lines('text = "a # b"\n') == []
    assert comment_guard.strip_python("def f():\n    \"\"\"only\"\"\"\n") == "def f():\n    pass\n"
    assert comment_guard.strip_python('value = "a # b"  # gone\n') == 'value = "a # b"\n'


def test_powershell_here_string_is_not_a_comment():
    assert comment_guard.powershell_issue_lines('@\"\n### title\n\"@\n') == []
    assert comment_guard.powershell_issue_lines("# hi\n$x = 1\n") == [(1, "comment")]


def test_inno_preprocessor_is_not_a_comment():
    text = "; note\n#ifndef AppVersion\n  // silent\n#endif\n"
    assert comment_guard.inno_issue_lines(text) == [(1, "comment"), (3, "comment")]
    stripped = comment_guard.strip_text("inno", text)
    assert "#ifndef AppVersion" in stripped
    assert "#endif" in stripped
    assert comment_guard.inno_issue_lines(stripped) == []


def test_gitignore_tracks_sample_bars_and_ignores_market_data():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.csv.gz" in text.splitlines()
    assert "!assets/sample/**/*.csv.gz" in text.splitlines()
    assert comment_guard.gitignore_issue_lines(text) == []


def test_gui_workflow_pins_ubuntu_2404_for_qt_library_names():
    text = (ROOT / ".github" / "workflows" / "gui.yml").read_text(encoding="utf-8")
    reason = "ubuntu-latest is 26.04 and its Qt library names do not match this install step"
    assert "os: [ubuntu-24.04, windows-latest]" in text, reason
    assert "ubuntu-latest" not in text, reason


def test_pack_day_cuts_are_not_named_run_defaults():
    for name in ("runner.py", "parallel.py", "__main__.py"):
        text = (ROOT / "src" / "backtester" / name).read_text(encoding="utf-8")
        assert "TRAIN_LAST_DAY" not in text
        assert "DESIGN_LAST_DAY" not in text
        assert re.search(r"\bWINDOWS\b", text) is None
