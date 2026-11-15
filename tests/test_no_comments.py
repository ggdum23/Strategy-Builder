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
    assert comment_guard.is_documentation("README.md")
    assert comment_guard.is_documentation("docs/PERFORMANCE.md")
    assert comment_guard.is_documentation("docs/screenshots/desk.png")
    assert not comment_guard.is_documentation("docs/helper.py")
    assert not comment_guard.is_documentation("docs/tool.sh")
    assert not comment_guard.is_documentation("src/backtester/engine_arena.py")
    assert comment_guard.issues_for("docs/helper.py", "x = 1  # hidden\n") == [
        comment_guard.Finding("docs/helper.py", 1, "comment")
    ]
    assert comment_guard.issues_for("docs/PERFORMANCE.md", "x = 1  # prose\n") == []


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
    assert comment_guard.python_issue_lines('x = """hidden"""\n') == []
    hidden = '"""hidden"""\n'
    assert comment_guard.python_issue_lines("from __future__ import annotations\n" + hidden) == [(2, "docstring")]
    assert comment_guard.python_issue_lines("x = 1\n" + hidden) == [(2, "docstring")]
    assert comment_guard.python_issue_lines("class C:\n    x = 1\n    " + hidden) == [(3, "docstring")]
    assert comment_guard.python_issue_lines("def f():\n    x = 1\n    " + hidden) == [(3, "docstring")]
    assert comment_guard.strip_python("from __future__ import annotations\n" + hidden + "x = 1\n") == (
        "from __future__ import annotations\nx = 1\n"
    )
    assert comment_guard.strip_python("def f():\n    x = 1\n    " + hidden + "    return x\n") == (
        "def f():\n    x = 1\n    return x\n"
    )


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


def test_unknown_code_suffixes_fail_closed_and_python_windows_files_are_scanned():
    assert comment_guard.issues_for("tool.pyw", "x = 1  # note\n") == [
        comment_guard.Finding("tool.pyw", 1, "comment")
    ]
    assert comment_guard.issues_for("src/extra.c", "int x;\n") == [
        comment_guard.Finding("src/extra.c", 1, "unscanned")
    ]
    assert comment_guard.issues_for("assets/sample/bars.csv", "a,b\n") == []
    assert comment_guard.issues_for("LICENSE.txt", "plain prose\n") == []
    assert comment_guard.hash_config_issue_lines("value = '# not a comment'\n# real\n") == [(2, "comment")]


def test_workflow_run_blocks_scan_embedded_python_docstrings():
    heredoc = "run: |\n  python - <<'PY'\n  \"\"\"hidden\"\"\"\n  x = 1\n  PY\n"
    assert comment_guard.yaml_issue_lines(heredoc) == [(3, "docstring")]
    python_shell = "- shell: python\n  run: |\n    \"\"\"hidden\"\"\"\n    x = 1\n"
    assert comment_guard.yaml_issue_lines(python_shell) == [(3, "docstring")]
    bash = "run: |\n  echo hello\n"
    assert comment_guard.yaml_issue_lines(bash) == []


def test_sandbox_header_constants_keep_their_values():
    from backtester import sandbox

    text = (ROOT / "src" / "backtester" / "sandbox.py").read_text(encoding="utf-8")
    assert sandbox.FWP_MATCH_EQUAL == 0
    assert sandbox.FWP_SID == 13
    assert sandbox.FWP_UINT64 == 4
    assert sandbox.FWP_ACTION_BLOCK == 0x1001
    assert sandbox.SE_FILE_OBJECT == 1
    assert sandbox.DACL_SECURITY_INFORMATION == 4
    assert sandbox.OWNER_SECURITY_INFORMATION == 1
    assert sandbox.FWPM_SESSION_FLAG_DYNAMIC == 0x1
    assert sandbox.RPC_C_AUTHN_WINNT == 10
    assert sandbox.FILE_TRAVERSE == 0x20
    assert sandbox.FILE_READ_ATTRIBUTES == 0x80
    assert sandbox.SYNCHRONIZE == 0x100000
    assert sandbox.GRANT_ACCESS == 1
    assert sandbox.REVOKE_ACCESS == 4
    assert sandbox.TRUSTEE_IS_SID == 0
    assert sandbox._LANDLOCK_READ_DIR == 1 << 3
    assert sandbox._LANDLOCK_READ == (1 << 0) | (1 << 2) | sandbox._LANDLOCK_READ_DIR
    write_bits = (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 13) | (1 << 14)
    assert sandbox._LANDLOCK_WRITE == sandbox._LANDLOCK_READ | write_bits
    assert "condition.matchType = FWP_MATCH_EQUAL" in text
    assert "condition.value.type = FWP_SID" in text
    assert "filt.weight.type = FWP_UINT64" in text
    assert "filt.action.type = FWP_ACTION_BLOCK" in text
    assert "SE_FILE_OBJECT, DACL_SECURITY_INFORMATION" in text
    assert "SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION" in text
    assert "session.flags = FWPM_SESSION_FLAG_DYNAMIC" in text
    assert "RPC_C_AUTHN_WINNT" in text
    assert "FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE" in text
    assert "grfAccessMode = GRANT_ACCESS" in text
    assert "grfAccessMode = REVOKE_ACCESS" in text
    assert "TrusteeForm = TRUSTEE_IS_SID" in text
    assert "_LANDLOCK_READ_DIR" in text


def test_pack_day_cuts_are_not_named_run_defaults():
    for name in ("runner.py", "parallel.py", "__main__.py"):
        text = (ROOT / "src" / "backtester" / name).read_text(encoding="utf-8")
        assert "TRAIN_LAST_DAY" not in text
        assert "DESIGN_LAST_DAY" not in text
        assert re.search(r"\bWINDOWS\b", text) is None
