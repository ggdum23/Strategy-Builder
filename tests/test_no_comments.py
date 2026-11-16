from __future__ import annotations

import hashlib
import random
import re
import warnings
from pathlib import Path

from backtester import comment_guard

ROOT = Path(__file__).resolve().parents[1]
PINNED_COSTS_SHA256 = "a3b0a522d8bf469001e75aaf274b0c5d3cbef624a68ed9fb94d29298110fe281"
PINNED_COSTS_DESK_SHA256 = "07b0d25ceeebe292f03a2b7b6ca0d76d76fa8d6a59f21d60103abfceeab0ebbd"


def test_threat_model_rejects_unused_commentary_and_unmodeled_syntax():
    assert comment_guard.THREAT_MODEL == (
        "reject any construct that stores unused commentary text in tracked files",
        "treat every unmodeled file type, key, executable or syntax as a finding",
        "flag a binding of __doc__ or an all-underscore name other than a bare _ in case, for, or a call assignment, a namespace or __dict__ write whose key is not a different constant, and exec, eval, compile, or an alias of setattr, globals, vars, locals, exec, or eval",
    )
    assert comment_guard.DYNAMIC_CALL_ALLOWLIST == (
        ("compile", "source", "<generated>", "exec"),
    )
    assert comment_guard.EXECUTABLE_ALLOWLIST == ("apt-get", "python", "sudo")
    assert comment_guard.COMMAND_ALLOWLIST == (
        ("apt-get", "install", "-y", "bubblewrap"),
        ("apt-get", "update"),
        ("python", "-m", "backtester", "test"),
        ("python", "-m", "pip", "install", "--upgrade", "pip"),
        ("python", "-m", "pip", "install", "-e", ".[dev,gui]"),
        ("python", "-m", "pip", "install", "-e", ".[dev]"),
        ("python", "-m", "pip", "install", "-r", "packaging/requirements-build.txt"),
        ("python", "-m", "pip", "install", "."),
        ("python", "-m", "pytest", "-q", "-m", "jailed_sample", "--timeout=60", "--timeout-method=thread", "tests/test_jailed_examples.py"),
        ("python", "-m", "pytest", "-q", "-m", "jailed_sample", "tests/test_jailed_examples.py"),
        ("python", "-m", "pytest", "-q", "tests/gui"),
        ("python", ".github/install_qt_libraries.py"),
        ("python", "packaging/build_windows.py"),
        ("python", "packaging/ci_delete_userdata.py"),
        ("python", "packaging/ci_install.py"),
        ("python", "packaging/ci_uninstall.py"),
        ("python", "packaging/ci_upgrade.py"),
        ("python", "packaging/record_setup.py"),
        ("python", "packaging/smoke_frozen.py", "--repo", "."),
        ("python", "scripts/bench.py", "--scale", "ci", "--gate"),
        ("python", "tests/ci_shards.py", "plan"),
        ("python", "tests/ci_shards.py", "run"),
    )
    assert comment_guard.COMMAND_ALLOWLIST == tuple(sorted(comment_guard.COMMAND_ALLOWLIST))
    assert "script" not in comment_guard.KNOWN_KEYS["with"]
    assert "entrypoint" not in comment_guard.KNOWN_KEYS["step"]
    assert "args" not in comment_guard.KNOWN_KEYS["step"]


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
    assert not comment_guard.is_documentation("docs/art.svg")
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


def test_unmodeled_script_files_fail_closed():
    here = '@"\n### title\n"@\n'
    assert comment_guard.issues_for("tool.ps1", here) == [
        comment_guard.Finding("tool.ps1", 1, "unscanned")
    ]
    assert comment_guard.issues_for("tool.ps1", "python -c 'x = 1 # hidden'\npython3.12 -c 'x = 1'\n") == [
        comment_guard.Finding("tool.ps1", 1, "unscanned")
    ]
    assert comment_guard.issues_for("tool.bat", "echo hi & rem hidden\n") == [
        comment_guard.Finding("tool.bat", 1, "unscanned")
    ]
    assert comment_guard.issues_for("tool.cmd", "rem hidden\n") == [
        comment_guard.Finding("tool.cmd", 1, "unscanned")
    ]
    assert comment_guard.issues_for("a.css", 'a { content: "/* star */"; }\n') == [
        comment_guard.Finding("a.css", 1, "unscanned")
    ]
    assert comment_guard.issues_for("a.css", "/* hidden */\n") == [
        comment_guard.Finding("a.css", 1, "unscanned")
    ]
    assert comment_guard.issues_for("a.css", "body { color: red } // hidden\n") == [
        comment_guard.Finding("a.css", 1, "unscanned")
    ]
    assert comment_guard.issues_for("page.js", '"http://example.com"\n') == [
        comment_guard.Finding("page.js", 1, "unscanned")
    ]
    assert comment_guard.issues_for("docs/tool.sh", "# hidden\n") == [
        comment_guard.Finding("docs/tool.sh", 1, "unscanned")
    ]


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
    assert _has_kind(heredoc, "inline-python")
    python_shell = "- shell: python\n  run: |\n    \"\"\"hidden\"\"\"\n    x = 1\n"
    assert _has_kind(python_shell, "inline-python")
    bash = "run: |\n  python -m backtester test\n"
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
    assert sandbox.FWPM_LAYER_ALE_AUTH_CONNECT_V4 == (0xC38D57D1, 0x05A7, 0x4C33, (0x90, 0x4F, 0x7F, 0xBC, 0xEE, 0xE6, 0x0E, 0x82))
    assert sandbox.FWPM_LAYER_ALE_AUTH_CONNECT_V6 == (0x4A72393B, 0x319F, 0x44BC, (0x84, 0xC3, 0xBA, 0x54, 0xDC, 0xB3, 0xB6, 0xB4))
    assert sandbox.FWPM_SUBLAYER_UNIVERSAL == (0xEEBECC03, 0xCED4, 0x4380, (0x81, 0x9A, 0x27, 0x34, 0x39, 0x7B, 0x2B, 0x74))
    assert sandbox.FWPM_CONDITION_ALE_PACKAGE_ID == (0x71BC78FA, 0xF17C, 0x4997, (0xA6, 0x02, 0x6A, 0xBB, 0x26, 0x1F, 0x35, 0x1C))
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
    assert "guid(*FWPM_LAYER_ALE_AUTH_CONNECT_V4)" in text
    assert "guid(*FWPM_LAYER_ALE_AUTH_CONNECT_V6)" in text
    assert "guid(*FWPM_SUBLAYER_UNIVERSAL)" in text
    assert "guid(*FWPM_CONDITION_ALE_PACKAGE_ID)" in text
    assert "return (FWPM_LAYER_ALE_AUTH_CONNECT_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V6)" in text


def test_pack_day_cuts_are_not_named_run_defaults():
    for name in ("runner.py", "parallel.py", "__main__.py"):
        text = (ROOT / "src" / "backtester" / name).read_text(encoding="utf-8")
        assert "TRAIN_LAST_DAY" not in text
        assert "DESIGN_LAST_DAY" not in text
        assert re.search(r"\bWINDOWS\b", text) is None


def _has_kind(text: str, kind: str) -> bool:
    return any(item_kind == kind for _line, item_kind in comment_guard.yaml_issue_lines(text))


def test_discarded_expression_statements_are_flagged():
    samples = [
        'f"""hidden comment"""\n',
        'f"""hidden {1}"""\n',
        'rf"""hidden"""\n',
        'b"""hidden comment"""\n',
        'rb"hidden"\n',
        'b"hid" b"den"\n',
        '("""hidden comment""",)\n',
        '["hidden comment"]\n',
        '{"hidden"}\n',
        '{"note": "hidden"}\n',
        "name\n",
        "obj.attr\n",
        "...\n",
        "def f():\n    x = 1\n    ...\n",
        '"""hidden docstring""" if True else print(0)\n',
        'print(0) if False else """hidden docstring"""\n',
        '"""hidden docstring""" or print(0)\n',
        'f"""hidden {1}""" if True else len("x")\n',
        'b"""hidden""" if True else len(b"x")\n',
        '("""hidden""",) if True else len("x")\n',
        'print(0) and """hidden docstring"""\n',
        '(lambda: """hidden docstring""")\n',
        '(lambda: """hidden docstring""")() and """hidden"""\n',
        '("""hidden""" for i in range(1))\n',
        '["""hidden""" for i in range(1)]\n',
        "(x := 1)\n",
    ]
    for sample in samples:
        assert comment_guard.python_issue_lines(sample), sample
    reason = "an ellipsis is the empty body of a function or class, and pass is the other spelling"
    assert comment_guard.python_issue_lines("def f():\n    ...\n") == [], reason
    assert comment_guard.python_issue_lines("async def f():\n    ...\n") == [], reason
    assert comment_guard.python_issue_lines("class C:\n    ...\n") == [], reason
    assert comment_guard.python_issue_lines("name()\n") == []
    assert comment_guard.python_issue_lines("(lambda: name())()\n") == []
    assert comment_guard.python_issue_lines('(lambda: """hidden docstring""")()\n') == []
    assert comment_guard.python_issue_lines("def f():\n    yield 1\n") == []
    assert comment_guard.python_issue_lines("def f():\n    yield from name()\n") == []
    assert comment_guard.python_issue_lines("async def f():\n    await name()\n") == []


def test_generated_no_effect_expression_statements_are_flagged():
    rng = random.Random(15)
    atoms = [
        "0",
        "None",
        "True",
        "...",
        "'text'",
        '"text"',
        '"""hidden comment"""',
        "b'hid'",
        'b"""hidden comment"""',
        "rb'hidden'",
        'b"hid" b"den"',
        'f"hidden"',
        'f"""hidden comment"""',
        'f"""hidden {1}"""',
        'rf"""hidden"""',
        "name",
        "obj.attr",
        "obj.attr.other",
    ]

    def compose(depth: int) -> str:
        if depth >= 3 or rng.randrange(4) == 0:
            return rng.choice(atoms)
        kind = rng.choice(["paren", "tuple", "list", "dict", "attr", "subscript", "binop", "compare", "ifexp", "lambda", "gen", "set"])
        inner = compose(depth + 1)
        if kind == "paren":
            return f"({inner})"
        if kind == "tuple":
            return f"({inner}, {compose(depth + 1)})"
        if kind == "list":
            return f"[{inner}, {compose(depth + 1)}]"
        if kind == "dict":
            return "{'k': " + inner + "}"
        if kind == "attr":
            return f"({inner}).attr"
        if kind == "subscript":
            return f"({inner})[0]"
        if kind == "binop":
            return f"({inner} + {compose(depth + 1)})"
        if kind == "compare":
            return f"({inner} == {compose(depth + 1)})"
        if kind == "ifexp":
            return f"({inner} if flag else {compose(depth + 1)})"
        if kind == "lambda":
            return f"(lambda: {inner})"
        if kind == "gen":
            return f"({inner} for item in items)"
        return "{" + inner + "}"

    produced = 0
    for _ in range(80):
        expr = compose(0)
        for source in (expr + "\n", "def f():\n    value = 1\n    " + expr + "\n"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    compile(source, "<generated>", "exec")
            except SyntaxError:
                continue
            findings = comment_guard.python_issue_lines(source)
            assert findings, source
            produced += 1
    assert produced >= 80
    allowed = [
        "name()\n",
        "obj.method()\n",
        "(lambda: name())()\n",
        "def f():\n    yield 1\n",
        "def f():\n    yield from name()\n",
        "async def f():\n    await name()\n",
        "def f():\n    ...\n",
        "class C:\n    ...\n",
    ]
    rejected = [
        "name() + 1\n",
        "(x := 1)\n",
        "name() if flag else other\n",
        "(lambda x=name(): x)\n",
        "(item for item in name())\n",
        'f"{name()}"\n',
        "[name() for item in items]\n",
        '"""hidden""" if True else print(0)\n',
        'print(0) and """hidden"""\n',
    ]
    for source in allowed:
        assert comment_guard.python_issue_lines(source) == [], source
    for source in rejected:
        assert comment_guard.python_issue_lines(source), source
    binding = random.Random(6)
    names = ["__doc__", "__", "___"]
    forms = [
        "match value:\n    case {name}:\n        pass\n",
        "match value:\n    case 'hidden' as {name}:\n        pass\n",
        "match [value]:\n    case [{name}]:\n        pass\n",
        "match {{'a': 1}}:\n    case {{**{name}}}:\n        pass\n",
        "class A:\n    match value:\n        case {name}:\n            pass\n",
        "def {name}():\n    pass\n",
        "def f({name}):\n    pass\n",
        "for {name} in items:\n    pass\n",
        "{name} = 'hidden'\n",
        "import mod as {name}\n",
        "x = [1 for {name} in items]\n",
        "def f():\n    global {name}\n",
    ]
    flagged = 0
    for _index in range(36):
        source = binding.choice(forms).format(name=binding.choice(names))
        assert comment_guard.python_issue_lines(source), source
        flagged += 1
    assert flagged >= 36


def test_workflow_inline_python_and_yaml_comments_fail_closed():
    indented = "run: |\n  python - <<'PY'\n    \"\"\"hidden\"\"\"\n    x = 1\n  PY\n"
    assert _has_kind(indented, "inline-python")
    colon = "run: |\n  python - <<'PY'\n  x = 1  # hidden\n  :\n  PY\n"
    assert _has_kind(colon, "inline-python")
    explicit = "- shell: python\n  run: |2\n    \"\"\"hidden\"\"\"\n"
    assert _has_kind(explicit, "inline-python")
    shell_after = "- run: |\n    \"\"\"hidden\"\"\"\n  shell: python\n"
    assert _has_kind(shell_after, "inline-python")
    unbuffered = "- shell: python -u\n  run: |\n    \"\"\"hidden\"\"\"\n"
    assert _has_kind(unbuffered, "inline-python")
    command = "run: |\n  python -c 'x = 1  # hidden'\n"
    assert _has_kind(command, "inline-python")
    defaults = "jobs:\n  test:\n    defaults:\n      run:\n        shell: python\n    steps:\n      - run: |\n          \"\"\"hidden\"\"\"\n"
    assert _has_kind(defaults, "inline-python")
    written = "run: |\n  cat > tool.py << 'PY'\n  \"\"\"hidden\"\"\"\n  PY\n"
    assert _has_kind(written, "inline-python")
    data = "run: |\n  cat <<'EOF'\n  # not a comment\n  EOF\n"
    assert _has_kind(data, "inline-python") or _has_kind(data, "comment")
    assert comment_guard.yaml_issue_lines("name: ci # hidden\n") == [(1, "comment")]
    assert comment_guard.yaml_issue_lines('name: "echo # no"\n') == []
    assert comment_guard.yaml_issue_lines("run: |\n  echo hello # hidden\n") == [(2, "comment"), (2, "inline-python")]
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines("a: &anchor 1\n"))


def test_config_comments_are_case_insensitive_and_cover_ini_and_markup():
    assert comment_guard.issues_for("Requirements.txt", "# hidden\n") == [
        comment_guard.Finding("Requirements.txt", 1, "comment")
    ]
    assert comment_guard.issues_for("requirements.TXT", "# hidden\n") == [
        comment_guard.Finding("requirements.TXT", 1, "comment")
    ]
    assert comment_guard.issues_for("setup.cfg", "; hidden\n") == [
        comment_guard.Finding("setup.cfg", 1, "comment")
    ]
    assert comment_guard.issues_for("app.INI", "; hidden\n") == [
        comment_guard.Finding("app.INI", 1, "comment")
    ]
    assert comment_guard.issues_for("setup.cfg", "key = 'a; b'\n") == []
    assert comment_guard.issues_for("art.svg", "<svg><!-- hidden --></svg>\n") == [
        comment_guard.Finding("art.svg", 1, "comment")
    ]
    assert comment_guard.issues_for("docs/art.svg", "<!-- hidden -->\n") == [
        comment_guard.Finding("docs/art.svg", 1, "comment")
    ]
    assert comment_guard.issues_for("note.xml", "<!-- hidden -->\n") == [
        comment_guard.Finding("note.xml", 1, "comment")
    ]
    assert comment_guard.issues_for("page.HTML", "<!-- hidden -->\n") == [
        comment_guard.Finding("page.HTML", 1, "comment")
    ]
    assert comment_guard.issues_for("constraints.txt", "# hidden\n") == [
        comment_guard.Finding("constraints.txt", 1, "comment")
    ]
    assert comment_guard.issues_for("dev-requirements.txt", "pkg # hidden\n") == [
        comment_guard.Finding("dev-requirements.txt", 1, "comment")
    ]
    assert comment_guard.issues_for("notes.txt", "plain\n") == [
        comment_guard.Finding("notes.txt", 1, "unscanned")
    ]
    assert any(item.kind == "unscanned" for item in comment_guard.issues_for("art.svg", "<svg><script>/* hidden */</script></svg>\n"))
    assert any(item.kind == "unscanned" for item in comment_guard.issues_for("page.html", "<script>// hidden</script>\n"))
    assert any(item.kind == "unscanned" for item in comment_guard.issues_for("page.html", '<div style="color:red"></div>\n'))
    assert any(item.kind == "unscanned" for item in comment_guard.issues_for("art.svg", '<svg onclick="hide"></svg>\n'))
    assert comment_guard.issues_for("Makefile", "all:\n") == [comment_guard.Finding("Makefile", 1, "unscanned")]
    assert comment_guard.issues_for("Dockerfile", "FROM python\n") == [comment_guard.Finding("Dockerfile", 1, "unscanned")]
    assert comment_guard.issues_for("costs.py.bak", "x = 1\n") == [comment_guard.Finding("costs.py.bak", 1, "unscanned")]
    assert comment_guard.issues_for("src\\backtester\\costs.py", "x = 1  # hidden\n") == [
        comment_guard.Finding("src\\backtester\\costs.py", 1, "comment")
    ]
    assert comment_guard.issues_for("src/backtester/notes.md", "# prose\n") == []


def test_workflow_grammar_blocks_the_review_bypasses():
    substitution = "run: |\n  echo $(\n  # hidden comment\n  echo hi)\n"
    assert _has_kind(substitution, "comment")
    duplicate_run = "steps:\n  - run: echo ok\n    run: |\n      echo hi # hidden comment\n"
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(duplicate_run))
    duplicate_shell = "steps:\n  - shell: bash\n    shell: python\n    run: |\n      \"\"\"hidden\"\"\"\n"
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(duplicate_shell))
    assert _has_kind(duplicate_shell, "inline-python")
    duplicate_quote = 'steps:\n  - run: echo ok\n    run: "echo hi # hidden comment"\n'
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(duplicate_quote))
    for script in (
        "python3.12 -c 'x = 1 # hidden'\n",
        "/usr/bin/python3.12 -c 'x = 1'\n",
        "pythonw -c 'x = 1'\n",
        "pythonw.exe -c 'x = 1'\n",
        "pypy3 -c 'x = 1'\n",
        "bash -c 'echo hi # hidden'\n",
        "sh -c 'echo hi'\n",
        "eval 'python -c \"x = 1 # hidden\"'\n",
        "bash <<'EOF'\npython -c 'x = 1'\nEOF\n",
        "python3.12 - <<'PY'\n\"\"\"hidden\"\"\"\nPY\n",
        "bash <<'EOF'\necho hi # hidden\nEOF\n",
        "bash <<'EOF'\nbash <<'EOF'\necho hi\nEOF\nEOF\n",
        "dest=tool.py\ncat >\"$dest\" <<'PY'\n",
        "pwsh -Command 'echo hi'\n",
        "python -cprint\n",
        "echo hi | python -\n",
        "cat >\"$dest\" <<'PY'\n",
        "echo ${#array}\n",
        "echo $#\n",
        "echo ${var#prefix}\n",
        "python -m pip install -c constraints.txt\n",
    ):
        document = "run: |\n" + "".join(f"  {line}" for line in script.splitlines(True))
        assert comment_guard.yaml_issue_lines(document), script
    assert _has_kind("steps:\n  - shell: python3.12\n    run: echo ok\n", "inline-python")
    assert _has_kind("defaults:\n  run:\n    shell: python3.12\n", "inline-python")
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines("run: {echo: ok, echo: hidden}\n"))
    assert comment_guard.yaml_issue_lines("run: echo hello\n")
    assert comment_guard.yaml_issue_lines("run: python -m backtester test\n") == []
    assert comment_guard.yaml_issue_lines("run: python3 script.py\n")
    assert comment_guard.yaml_issue_lines("shell: pwsh\nrun: python packaging/build_windows.py\n") == []
    assert comment_guard.yaml_issue_lines("shell: Bash\nrun: python -m backtester test\n") == []
    assert comment_guard.yaml_issue_lines("shell: cmd\nrun: python packaging/build_windows.py\n") == []
    assert comment_guard.yaml_issue_lines("shell: cmd\nrun: python script.py\n")
    assert comment_guard.yaml_issue_lines('run: echo "hello # world"\n')
    assert comment_guard.yaml_issue_lines('run: python -m pip install -e ".[dev]"\n') == []
    assert comment_guard.yaml_issue_lines('run: python -m pip install -e ".[dev,gui]"\n') == []
    assert comment_guard.yaml_issue_lines("run: sudo apt-get update && sudo apt-get install -y bubblewrap\n") == []
    assert comment_guard.yaml_issue_lines("run: echo a,b\n")


def test_generated_run_lines_outside_the_grammar_are_flagged():
    rng = random.Random(15)
    safe = ["python", "sudo", "echo", "apt-get", "-m", "pip", "install", "-y", "script.py", "packaging/build_windows.py"]
    unsafe = ["#", "$(", "`", "<<", "${", "-c", "eval", "bash", "sh", "dash", ">", "&"]
    flagged = 0
    for index in range(40):
        words = [rng.choice(safe) for _ in range(3)]
        if index % 2 == 0:
            words.append(rng.choice(unsafe))
        line = " ".join(words)
        document = "run: |\n  " + line + "\n"
        findings = comment_guard.yaml_issue_lines(document)
        allowed = comment_guard._run_line_allowed(line)
        if allowed:
            assert findings == [], line
        else:
            assert findings, line
            flagged += 1
    executables = ["node", "bun", "php", "osascript", "true", "rem", "cmd", "env", "echo", "bash", "perl", "ruby", "deno", "pwsh"]
    for _index in range(20):
        executable = rng.choice(executables)
        assert executable not in comment_guard.EXECUTABLE_ALLOWLIST
        assert comment_guard.yaml_issue_lines("run: " + executable + " hidden\n"), executable
        flagged += 1
    for _index in range(20):
        key = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(4, 9)))
        if key in comment_guard.KNOWN_KEYS["root"]:
            continue
        assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(key + ": hidden\n")), key
        flagged += 1
    extras = ["hidden", "comment", "version", "list", "freeze", "debug", "words"]
    for command in comment_guard.COMMAND_ALLOWLIST:
        extended = command + (rng.choice(extras),)
        assert extended not in comment_guard.COMMAND_ALLOWLIST
        assert comment_guard.yaml_issue_lines("run: " + " ".join(extended) + "\n"), extended
        flagged += 1
    assert flagged >= 40


def test_review4_finite_allowlist_closes_the_filed_rows():
    script = "steps:\n  - uses: actions/github-script@v7\n    with:\n      script: |\n        // hidden comment\n"
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(script))
    script_hash = "steps:\n  - with:\n      script: |\n        # hidden\n"
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(script_hash))
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines('script: "x = 1 # hidden"\n'))
    entrypoint = "entrypoint: |\n  # hidden\n  python app.py\n"
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(entrypoint))
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines("args: echo hi\n"))
    path = "steps:\n  - with:\n      path: |\n        # hidden\n"
    assert any(kind == "comment" for _line, kind in comment_guard.yaml_issue_lines(path))
    cache = "steps:\n  - with:\n      cache-dependency-path: |\n        # hidden\n"
    assert any(kind == "comment" for _line, kind in comment_guard.yaml_issue_lines(cache))
    for command in (
        "rem hidden comment",
        "REM hidden comment",
        ":: hidden comment",
        "echo ok && rem hidden comment",
        "cmd /c rem hidden comment",
        "cmd.exe /c rem hidden comment",
        "node -e 'console.log(1) // hidden'",
        "node --eval '// hidden'",
        "node -p '1 // hidden'",
        "node -e 'console.log(1) /* hidden */'",
        "env node -e '// hidden'",
        "bun -e 'console.log(1) // hidden'",
        "php -r 'echo 1; // hidden'",
        "php -r 'echo 1; /* hidden */'",
        "osascript -e '1 -- hidden'",
        ": this is a hidden comment",
        "echo ok && : hidden comment",
        "true this is a hidden comment",
        "false this is a hidden comment",
        "python -e 'code'",
        "python --eval 'code'",
        "python -p 'code'",
        "python -r 'code'",
        "python -c 'x = 1'",
    ):
        document = "shell: cmd\nrun: " + command + "\n"
        assert comment_guard.yaml_issue_lines(document), command
    assert comment_guard.yaml_issue_lines("shell: bash\nrun: cmd /c rem hidden comment\n")
    assert comment_guard.yaml_issue_lines("shell: pwsh\nrun: cmd.exe /c rem hidden comment\n")
    assert comment_guard.yaml_issue_lines("run: python packaging/tool.py -e code\n")
    assert comment_guard.yaml_issue_lines("run: python -m pytest -r code\n")
    assert comment_guard.yaml_issue_lines("run: python -m pip install -r packaging/requirements-build.txt\n") == []
    assert comment_guard.yaml_issue_lines("run: python -m pip install --upgrade pip\n") == []
    assert comment_guard.yaml_issue_lines("run: python -m pip install .\n") == []
    assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines("description: |\n  # heading\n"))
    branches = "on:\n  push:\n    branches:\n      - |\n        # hidden\n"
    assert any(kind == "comment" for _line, kind in comment_guard.yaml_issue_lines(branches))
    assert comment_guard.python_issue_lines('__doc__ = """hidden docstring"""\n')
    assert comment_guard.python_issue_lines('class A:\n    __doc__ = "hidden"\n')
    assert comment_guard.python_issue_lines('def f():\n    f.__doc__ = "hidden"\n')
    assert comment_guard.python_issue_lines('globals()["__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('setattr(sys, "__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('setattr(sys, name="__doc__", value="hidden")\n')
    assert comment_guard.python_issue_lines('__doc__ += "hidden"\n')
    assert comment_guard.python_issue_lines('_ += b"hidden"\n')
    assert comment_guard.python_issue_lines('_ = """hidden"""\n')
    assert comment_guard.python_issue_lines('_ = f"""hidden {1}"""\n')
    assert comment_guard.python_issue_lines('_ = b"""hidden"""\n')
    assert comment_guard.python_issue_lines('_: str = "hidden"\n')
    assert comment_guard.python_issue_lines('(_,) = ("hidden",)\n')
    assert comment_guard.python_issue_lines("_ = print(1)\n") == []
    assert comment_guard.python_issue_lines('for _ in ("hidden",):\n    pass\n') == []
    assert comment_guard.python_issue_lines('exec("x = 1 # hidden")\n')
    assert comment_guard.python_issue_lines('exec("""hidden""")\n')
    assert comment_guard.python_issue_lines('compile("x = 1", "<s>", "exec")\n')
    assert comment_guard.python_issue_lines('x: """hidden""" = 1\n') == []
    assert comment_guard.python_issue_lines('async def f():\n    await "hidden"\n') == []
    assert comment_guard.python_issue_lines('def f():\n    yield "hidden"\n') == []
    assert comment_guard.python_issue_lines('"""hidden"""()\n') == []
    toml = 'key = "unterminated\n# hidden comment\n'
    assert any(kind == "unscanned" for _line, kind in comment_guard.hash_config_issue_lines(toml))
    requirements = 'pkg = "unterminated\n# hidden comment\n'
    assert any(item.kind == "unscanned" for item in comment_guard.issues_for("requirements.txt", requirements))
    cfg = 'key = "unterminated\n; hidden\n'
    assert any(kind == "unscanned" for _line, kind in comment_guard.ini_issue_lines(cfg))
    assert comment_guard.hash_config_issue_lines("key = '# not a comment'\n# real\n") == [(2, "comment")]
    manifest = (ROOT / "tests" / "reference" / "arena" / "ENGINE_SHA256.txt").read_text(encoding="utf-8")
    assert comment_guard.issues_for("tests/reference/arena/ENGINE_SHA256.txt", manifest) == []
    assert any(item.kind == "comment" for item in comment_guard.issues_for("ENGINE_SHA256.txt", manifest + "# hidden\n"))
    assert comment_guard.issues_for("ENGINE_SHA256.txt", "not a hash\n")
    digest = "a" * 64
    assert comment_guard.hash_manifest_issue_lines(digest + " file.py\n") == []
    assert comment_guard.hash_manifest_issue_lines(digest + "  file.py\n") == []
    assert comment_guard.hash_manifest_issue_lines(digest + "   file.py\n")
    assert comment_guard.hash_manifest_issue_lines(digest + "  #hidden\n")
    assert comment_guard.issues_for("LICENSE.txt", "# prose\n") == []
    assert comment_guard.issues_for("data.csv", "# hidden\n") == []
    package = '{"scripts": {"t": "echo hi # hidden"}}\n'
    assert comment_guard.issues_for("package.json", package) == [
        comment_guard.Finding("package.json", 1, "unscanned")
    ]
    assert comment_guard.json_issue_lines('{"name": "ok"}\n') == []
    assert comment_guard.issues_for("book.json", '{"scripts": {"t": "echo"}}\n')


def test_generated_executables_and_keys_outside_the_allowlist_are_flagged():
    rng = random.Random(15)
    executables = ["node", "bun", "php", "osascript", "true", "rem", "cmd", "env", "echo", "bash", "perl", "ruby", "deno", "pwsh"]
    flagged = 0
    for _index in range(20):
        executable = rng.choice(executables)
        line = executable + " hidden"
        assert executable not in comment_guard.EXECUTABLE_ALLOWLIST
        assert comment_guard.yaml_issue_lines("run: " + line + "\n"), line
        flagged += 1
    keys = ["script", "entrypoint", "args", "title", "description", "command", "workdir"]
    for _index in range(20):
        key = rng.choice(keys)
        assert key not in comment_guard.KNOWN_KEYS["root"]
        assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(key + ": hidden\n"))
        flagged += 1
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    produced = 0
    while produced < 20:
        key = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 8)))
        if key in comment_guard.KNOWN_KEYS["root"]:
            continue
        assert any(kind == "unscanned" for _line, kind in comment_guard.yaml_issue_lines(key + ": hidden\n")), key
        produced += 1
        flagged += 1
    assert flagged >= 40


def test_review5_structural_rules_close_the_filed_rows():
    assert comment_guard.python_issue_lines('__doc__ = "hidden" + " comment"\n')
    assert comment_guard.python_issue_lines('__doc__ = "hidden" if True else print(0)\n')
    assert comment_guard.python_issue_lines('__doc__ = "hidden" or print(0)\n')
    assert comment_guard.python_issue_lines('__doc__ = "hidden" * 1\n')
    assert comment_guard.python_issue_lines('__doc__ = "%s" % "hidden"\n')
    assert comment_guard.python_issue_lines('globals()[f"__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('globals()["__" + "doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('setattr(sys, f"__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('setattr(sys, "__doc__", "hid" + "den")\n')
    assert comment_guard.python_issue_lines('builtins.setattr(sys, "__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('object.__setattr__(sys, "__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('x = (__doc__ := "hidden")\n')
    assert comment_guard.python_issue_lines('x = __doc__ = "hid" + "den"\n')
    assert comment_guard.python_issue_lines('_ = "hidden" + " comment"\n')
    assert comment_guard.python_issue_lines('_ = "hidden" if True else print(0)\n')
    assert comment_guard.python_issue_lines('*_, = ("hidden",)\n')
    assert comment_guard.python_issue_lines('obj.__setitem__("__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('globals().__setitem__("__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('for __doc__ in ("hidden",):\n    pass\n')
    assert comment_guard.python_issue_lines("_ = print(1)\n") == []
    assert comment_guard.python_issue_lines('for _ in ("hidden",):\n    pass\n') == []
    assert comment_guard.python_issue_lines('exec("x = 1 # hidden")\n')
    assert comment_guard.python_issue_lines('globals()["name"] = "hidden"\n') == []
    assert comment_guard.python_issue_lines('setattr(sys, "version", "hidden")\n') == []
    assert comment_guard.python_issue_lines('monkeypatch.setattr(sys, "frozen", True)\n') == []
    assert comment_guard.python_issue_lines("async def f():\n    _ = await name()\n") == []
    assert comment_guard.python_issue_lines("def f():\n    _ = yield name()\n")
    assert comment_guard.python_issue_lines('vars()[f"__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('locals()["__" + "doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('builtins.globals()["__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('x = """hidden"""\n') == []
    for command in (
        "python -m pip list this is a hidden comment",
        "python -m pip freeze this is a hidden comment",
        "python -m pip --version this is a hidden comment",
        "python -m pip -V hidden commentary",
        "python -m pip debug this is a hidden comment",
        "python -m pytest --version this is a hidden comment",
        "sudo python -m pip --version hidden commentary",
        "python packaging/record_setup.py this is a hidden comment",
        "python -m pip install --constraint constraints.txt .",
    ):
        assert comment_guard.yaml_issue_lines("run: " + command + "\n"), command
    assert comment_guard.yaml_issue_lines('run: python -m pip install -e ".[dev]"\n') == []
    assert comment_guard.yaml_issue_lines("run: python -m pytest -q tests/gui\n") == []
    assert comment_guard.yaml_issue_lines("run: python -m backtester test\n") == []
    assert comment_guard.yaml_issue_lines("run: sudo apt-get update\n") == []
    assert comment_guard.yaml_issue_lines("run: sudo apt-get install -y bubblewrap\n") == []
    assert any(kind == "unscanned" for _line, kind in comment_guard.hash_config_issue_lines('key = """unterminated\n# hidden comment\n'))
    assert any(kind == "unscanned" for _line, kind in comment_guard.hash_config_issue_lines("key = '''unterminated\n# hidden comment\n"))
    assert any(item.kind == "unscanned" for item in comment_guard.issues_for("requirements.txt", 'pkg """\n# hidden comment\n'))
    assert any(kind == "unscanned" for _line, kind in comment_guard.ini_issue_lines('key = """unterminated\n; hidden comment\n'))
    assert comment_guard.hash_config_issue_lines('key = """closed"""\n# real\n') == [(2, "comment")]
    assert any(kind == "comment" for _line, kind in comment_guard.inno_issue_lines("[Code]\nbegin\n  { hidden comment }\nend\n"))
    assert any(kind == "comment" for _line, kind in comment_guard.inno_issue_lines("[Code]\nbegin\n  (* hidden comment *)\nend\n"))
    assert comment_guard.inno_issue_lines("#ifndef AppVersion\n") == []
    assert comment_guard.inno_issue_lines("[Setup]\nAppVersion={#AppVersion}\n") == []
    assert comment_guard.inno_issue_lines("[Code]\n  Result := '{#DefaultDataRoot}';\n") == []
    assert comment_guard.issues_for("book.json", '{"Scripts": {"build": "echo hi # hidden"}}\n')
    assert comment_guard.json_issue_lines('{"description": "echo hi # hidden"}\n') == []


def test_review6_bindings_close_the_filed_rows():
    assert comment_guard.python_issue_lines('match "hidden docstring":\n    case __doc__:\n        pass\n')
    assert comment_guard.python_issue_lines('match "hidden docstring":\n    case "hidden docstring" as __doc__:\n        pass\n')
    assert comment_guard.python_issue_lines('match ["hidden docstring"]:\n    case [__doc__]:\n        pass\n')
    assert comment_guard.python_issue_lines('match {"a": 1}:\n    case {**__doc__}:\n        pass\n')
    assert comment_guard.python_issue_lines('class A:\n    match "hidden":\n        case __doc__:\n            pass\n')
    assert comment_guard.python_issue_lines('match "hidden":\n    case __:\n        pass\n')
    assert comment_guard.python_issue_lines('vars(sys)[f"__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('vars(sys)["__" + "doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('builtins.vars(sys)[f"__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('vars(sys.modules["probe_mod"])[f"__doc__"] = "module-doc"\n')
    assert comment_guard.python_issue_lines('sys.__dict__[f"__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('mod.__dict__["__" + "doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('type.__setattr__(A, "__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('type.__setattr__(A, f"__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('type.__setattr__(A, "__" + "doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('x = [1 for __doc__ in ("hidden docstring",)]\n')
    assert comment_guard.python_issue_lines('class A:\n    x = [1 for __doc__ in ("hidden",)]\n')
    assert comment_guard.python_issue_lines('exec("__doc__ = \'hidden\'")\n')
    assert comment_guard.python_issue_lines('eval("__doc__")\n')
    assert comment_guard.python_issue_lines('operator.setitem(sys.__dict__, "__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('globals.__call__()[f"__doc__"] = "hidden"\n')
    assert comment_guard.python_issue_lines('s = setattr\ns(m, "__doc__", "hidden")\n')
    assert comment_guard.python_issue_lines('import sys as __doc__\n')
    assert comment_guard.python_issue_lines('def __doc__():\n    pass\n')
    assert comment_guard.python_issue_lines('def f(__doc__):\n    pass\n')
    assert comment_guard.python_issue_lines('match value:\n    case _:\n        pass\n') == []
    assert comment_guard.python_issue_lines('x = vars(sys)["version"]\n') == []
    assert comment_guard.python_issue_lines('vars(sys)["version"] = "hidden"\n') == []
    assert comment_guard.python_issue_lines('d[f"key"] = "text"\n') == []
    assert comment_guard.python_issue_lines('object.__setattr__(self, "_flag", threading.Event())\n') == []
    assert comment_guard.python_issue_lines('compile(source, "<generated>", "exec")\n') == []
    assert comment_guard.python_issue_lines('app.exec()\n') == []
    assert comment_guard.python_issue_lines('re.compile(r"a")\n') == []
    assert comment_guard.python_issue_lines("_ = print(1)\n") == []
    assert comment_guard.python_issue_lines('for _ in ("hidden",):\n    pass\n') == []


def test_decode_failures_are_findings(tmp_path: Path):
    import subprocess

    subprocess.check_call(["git", "init"], cwd=tmp_path)
    (tmp_path / "wide.py").write_bytes(b"\xff\xfe#\x00 \x00\n\x00")
    (tmp_path / "bad.py").write_bytes(b"\xff")
    (tmp_path / "link.py").symlink_to("missing.py")
    subprocess.check_call(["git", "add", "-A"], cwd=tmp_path)
    found = {item.path: item.kind for item in comment_guard.scan(tmp_path)}
    assert found.get("wide.py") == "unscanned"
    assert found.get("bad.py") == "unscanned"
    assert found.get("link.py") == "unscanned"
    assert comment_guard.python_issue_lines("\ufeff# hidden\n") == [(1, "unscanned")]
    assert comment_guard.python_issue_lines("x = 1\n\uff03 hidden\n") == [(1, "unscanned")]
    assert comment_guard.python_issue_lines("x = 1\n\u200b# hidden\n") == [(1, "unscanned")]
