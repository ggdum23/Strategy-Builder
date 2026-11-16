from __future__ import annotations

import ast
import json
import re
import subprocess
import tokenize
import unicodedata
from io import StringIO
from pathlib import Path

FILE_ALLOWLIST = (
    "src/backtester/costs.py",
    "src/backtester/costs_desk.py",
)

DIRECTIVE_ALLOWLIST: tuple[tuple[str, str], ...] = ()

THREAT_MODEL = (
    "reject any construct that stores unused commentary text in tracked files",
    "treat every unmodeled file type, key, executable or syntax as a finding",
    "flag a binding of an all-underscore name other than a bare _ in case, for, or a call assignment, and flag exec, eval, compile, or an alias of setattr, globals, vars, locals, exec, or eval",
    "any python identifier, attribute name, keyword name, or string constant exactly equal to __doc__ is a finding outside src/backtester/comment_guard.py and tests/test_no_comments.py",
    "globals, vars, and locals, including a builtins receiver, and an attribute named __dict__ are findings unless the use is a direct subscript read with a constant key",
    "the name __builtins__, an import of builtins with an alias, a from-import of setitem, ior, setattr, exec, eval, compile, globals, vars, or locals from operator or builtins, and a three-argument type call are findings",
    "a mixed assignment that discards a slot with _ stays allowed, including x = _ = a value, for B.__, and with as B.__; for (_,) over a literal of strings is a finding",
)

DYNAMIC_CALL_ALLOWLIST = (
    ("compile", "source", "<generated>", "exec"),
)

DOC_TOKEN_ALLOWLIST = (
    "src/backtester/comment_guard.py",
    "tests/test_no_comments.py",
)

TYPE_CALL_ALLOWLIST: tuple[tuple[str, ...], ...] = ()

_ALIAS_NAMES = frozenset({"setattr", "globals", "vars", "locals", "exec", "eval", "compile"})
_NAMESPACE_NAMES = frozenset({"globals", "vars", "locals"})
_DYNAMIC_CALLS = frozenset({"exec", "eval", "compile"})
_BUILTIN_MODULES = frozenset({"builtins", "__builtins__"})
_REFLECT_IMPORTS = frozenset({"setitem", "ior", "setattr", "exec", "eval", "compile", "globals", "vars", "locals"})
_LOOKUP_NAMES = frozenset({"getattr", "setattr", "hasattr", "delattr"})

EXECUTABLE_ALLOWLIST = ("apt-get", "python", "sudo")

COMMAND_ALLOWLIST = (
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

KNOWN_KEYS = {
    "root": ("concurrency", "defaults", "env", "jobs", "name", "on", "permissions", "run", "shell", "steps"),
    "on": ("pull_request", "push", "workflow_dispatch"),
    "push": ("branches", "tags"),
    "event": (),
    "concurrency": ("cancel-in-progress", "group"),
    "job": ("defaults", "env", "if", "name", "needs", "outputs", "permissions", "runs-on", "steps", "strategy", "timeout-minutes"),
    "strategy": ("fail-fast", "matrix"),
    "matrix": ("os", "python", "shard"),
    "step": ("env", "id", "if", "name", "run", "shell", "uses", "with"),
    "with": ("cache", "cache-dependency-path", "fail_on_unmatched_files", "files", "if-no-files-found", "name", "path", "python-version"),
    "defaults": ("run",),
    "defaults-run": ("shell",),
    "permissions": ("contents",),
    "outputs": ("shards",),
    "env": ("CI_SHARD", "MPLBACKEND", "PYTEST_QT_API", "QT_QPA_PLATFORM", "SB_INSTALL_DIR", "SB_SMOKE_DIR", "STRATEGY_BUILDER_HEADLESS"),
}

_KEY_NEST = {
    ("root", "on"): "on",
    ("root", "jobs"): "jobs",
    ("root", "concurrency"): "concurrency",
    ("root", "permissions"): "permissions",
    ("root", "env"): "env",
    ("root", "defaults"): "defaults",
    ("root", "steps"): "steps",
    ("root", "run"): "run",
    ("root", "shell"): "shell",
    ("on", "push"): "push",
    ("on", "pull_request"): "event",
    ("on", "workflow_dispatch"): "event",
    ("job", "strategy"): "strategy",
    ("job", "steps"): "steps",
    ("job", "env"): "env",
    ("job", "outputs"): "outputs",
    ("job", "permissions"): "permissions",
    ("job", "defaults"): "defaults",
    ("job", "run"): "run",
    ("job", "shell"): "shell",
    ("strategy", "matrix"): "matrix",
    ("step", "with"): "with",
    ("step", "env"): "env",
    ("step", "run"): "run",
    ("step", "shell"): "shell",
    ("defaults", "run"): "defaults-run",
    ("defaults-run", "shell"): "shell",
}

_HASH_SUFFIXES = {".toml"}
_PY_SUFFIXES = {".py", ".pyw", ".spec"}
_DOC_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
_DATA_SUFFIXES = {".csv", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".png", ".webp", ".xz", ".zip"}
_PROSE_TEXT_NAMES = {"license.txt", "install_before.txt"}
_HASH_MANIFEST_NAMES = {"engine_sha256.txt", "sha256sums"}
HASH_MANIFEST_LINE = re.compile(r"^[0-9a-f]{64}[ ]{1,2}\S+$")
_HTML_ACTIVE = re.compile(r"(?i)(?:<script|<style|(?:^|[\s<])style\s*=|(?<![A-Za-z0-9])on[a-z0-9:-]*\s*=)")
_KEY_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(.*)$")
_BLOCK_STYLE = re.compile(r"[|>](?:[+-][1-9]?|[1-9][+-]?)?$")


class Finding:
    def __init__(self, path: str, line: int, kind: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Finding):
            return False
        return (self.path, self.line, self.kind) == (other.path, other.line, other.kind)

    def __repr__(self) -> str:
        return f"Finding({self.path!r}, {self.line}, {self.kind!r})"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "backtester").is_dir():
            return parent
    raise RuntimeError("repo root not found")


def failure_message(findings: list[Finding]) -> str:
    shown = findings[:40]
    extra = len(findings) - len(shown)
    lines = ["comments are permitted only in costs.py and costs_desk.py"]
    lines.extend(f"{item.path}:{item.line}: {item.kind}" for item in shown)
    if extra:
        lines.append(f"{extra} more")
    return "\n".join(lines)


def _directive_allowed(path: str, text: str) -> bool:
    for allowed_path, snippet in DIRECTIVE_ALLOWLIST:
        if allowed_path == path and snippet == text:
            return True
    return False


def _newline_style(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_newlines(text: str, style: str) -> str:
    if style == "\r\n":
        return text.replace("\n", "\r\n")
    return text


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _position(starts: list[int], line: int, column: int) -> int:
    return starts[line - 1] + column


def _line_number(starts: list[int], offset: int) -> int:
    line = 1
    for index, start in enumerate(starts):
        if start <= offset:
            line = index + 1
        else:
            break
    return line


def _line_bounds(text: str, starts: list[int], line: int) -> tuple[int, int, int]:
    begin = starts[line - 1]
    if line < len(starts):
        end = starts[line]
        content_end = end - 1
    else:
        end = len(text)
        content_end = end
    return begin, content_end, end


def _expression_is_allowed(node: ast.AST) -> bool:
    return isinstance(node, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom))


def _bare_ellipsis(statement: ast.Expr, text: str, starts: list[int]) -> bool:
    value = statement.value
    if not isinstance(value, ast.Constant) or value.value is not Ellipsis:
        return False
    end_line = statement.end_lineno if statement.end_lineno is not None else statement.lineno
    end_column = statement.end_col_offset if statement.end_col_offset is not None else statement.col_offset
    start = _position(starts, statement.lineno, statement.col_offset)
    end = _position(starts, end_line, end_column)
    return text[start:end] == "..."


def _outside_is_whitespace(text: str, starts: list[int], start: int, end: int) -> bool:
    first = _line_number(starts, start)
    last = _line_number(starts, max(start, end - 1))
    for line in range(first, last + 1):
        begin, content_end, _full_end = _line_bounds(text, starts, line)
        left = max(begin, start)
        right = min(content_end, end)
        if text[begin:left].strip() or text[right:content_end].strip():
            return False
    return True


def _expand_lines(text: str, starts: list[int], start: int, end: int) -> tuple[int, int]:
    first = _line_number(starts, start)
    last = _line_number(starts, max(start, end - 1))
    begin, _content_end, _full_end = _line_bounds(text, starts, first)
    _last_begin, _last_content, full_end = _line_bounds(text, starts, last)
    return begin, full_end


def python_spans(text: str, *, ban_doc_token: bool = True) -> list[tuple[int, int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    spans: list[tuple[int, int, str]] = []
    try:
        tokens = tokenize.generate_tokens(StringIO(normalized).readline)
    except tokenize.TokenError as exc:
        raise ValueError(f"tokenize failed: {exc}") from exc
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        start = _position(starts, token.start[0], token.start[1])
        end = _position(starts, token.end[0], token.end[1])
        line_begin, _content_end, _full_end = _line_bounds(normalized, starts, token.start[0])
        while start > line_begin and normalized[start - 1] in " \t":
            start -= 1
        if _outside_is_whitespace(normalized, starts, start, end):
            start, end = _expand_lines(normalized, starts, start, end)
        spans.append((start, end, ""))
    tree = ast.parse(normalized)
    spans.extend(_no_effect_spans(normalized, starts, tree))
    spans.extend(_commentary_write_spans(normalized, starts, tree, ban_doc_token=ban_doc_token))
    spans.sort()
    return spans


def _no_effect_spans(text: str, starts: list[int], tree: ast.AST) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if not isinstance(body, list) or not body or not all(isinstance(item, ast.stmt) for item in body):
                continue
            allow_empty = isinstance(node, ast.Module) and field == "body"
            stub_ok = field == "body" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            spans.extend(_body_no_effect_spans(text, starts, body, allow_empty=allow_empty, stub_ok=stub_ok))
    return spans


def _const_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _underscore_name(name: str) -> bool:
    return bool(name) and set(name) == {"_"}


def _different_constant_name(node: ast.AST) -> bool:
    text = _const_text(node)
    return text is not None and text != "__doc__"


def _sensitive_name(name: str | None) -> bool:
    if not name:
        return False
    if name == "__doc__":
        return True
    return _underscore_name(name) and name != "_"


def _builtin_module(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in _BUILTIN_MODULES


def _namespace_func(func: ast.AST) -> bool:
    while isinstance(func, ast.Attribute) and func.attr == "__call__":
        func = func.value
    if isinstance(func, ast.Name):
        return func.id in _NAMESPACE_NAMES
    return isinstance(func, ast.Attribute) and func.attr in _NAMESPACE_NAMES and _builtin_module(func.value)


def _namespace_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _namespace_func(node.func)


def _is_dunder_dict(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "__dict__"


def _target_writes_doc(node: ast.AST) -> bool:
    if isinstance(node, ast.Starred):
        return _target_writes_doc(node.value)
    if isinstance(node, ast.Name):
        return node.id == "__doc__"
    if isinstance(node, ast.Attribute):
        return node.attr == "__doc__"
    if isinstance(node, ast.Subscript):
        if _namespace_call(node.value) or _is_dunder_dict(node.value):
            return not _different_constant_name(node.slice)
        return _const_text(node.slice) == "__doc__"
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_target_writes_doc(item) for item in node.elts)
    return False


def _target_is_only_underscores(node: ast.AST) -> bool:
    if isinstance(node, ast.Starred):
        return _target_is_only_underscores(node.value)
    if isinstance(node, ast.Name):
        return _underscore_name(node.id)
    if isinstance(node, ast.Attribute):
        return _underscore_name(node.attr)
    if isinstance(node, (ast.Tuple, ast.List)):
        return bool(node.elts) and all(_target_is_only_underscores(item) for item in node.elts)
    return False


def _assignment_targets(statement: ast.AST) -> list[ast.AST] | None:
    if isinstance(statement, ast.Assign):
        return list(statement.targets)
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        return [statement.target]
    if isinstance(statement, ast.NamedExpr):
        return [statement.target]
    return None


def _direct_call_or_await(node: ast.AST | None) -> bool:
    return isinstance(node, (ast.Call, ast.Await))


def _assignment_is_finding(statement: ast.AST) -> bool:
    targets = _assignment_targets(statement)
    if not targets:
        return False
    if any(_target_writes_doc(target) for target in targets):
        return True
    if all(_target_is_only_underscores(target) for target in targets):
        return not _direct_call_or_await(getattr(statement, "value", None))
    return False


def _call_argument(node: ast.Call, index: int, keyword: str) -> ast.AST | None:
    if len(node.args) > index:
        return node.args[index]
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return None


def _setter_kind(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name) and func.id == "setattr":
        return "setattr"
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "__setitem__":
        return "setitem"
    if func.attr == "__setattr__":
        return "setattr"
    owner = func.value
    if func.attr == "setattr" and _builtin_module(owner):
        return "setattr"
    return None


def _operator_setitem(func: ast.AST) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "setitem"
        and isinstance(func.value, ast.Name)
        and func.value.id == "operator"
    )


def _call_is_allowlisted(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Name) or node.keywords:
        return False
    for item in DYNAMIC_CALL_ALLOWLIST:
        name, *expected = item
        if func.id != name or len(node.args) != len(expected):
            continue
        matched = True
        for arg, want in zip(node.args, expected):
            if isinstance(arg, ast.Name) and arg.id == want:
                continue
            if isinstance(arg, ast.Constant) and arg.value == want:
                continue
            matched = False
            break
        if matched:
            return True
    return False


def _dynamic_exec_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _DYNAMIC_CALLS:
        return not _call_is_allowlisted(node)
    return isinstance(func, ast.Attribute) and func.attr in _DYNAMIC_CALLS and _builtin_module(func.value)


def _call_is_doc_write(node: ast.Call) -> bool:
    if _dynamic_exec_call(node):
        return True
    if _operator_setitem(node.func):
        key = _call_argument(node, 1, "key")
        if key is None:
            return True
        return not _different_constant_name(key)
    kind = _setter_kind(node.func)
    if kind is None:
        return False
    if kind == "setitem":
        name = _call_argument(node, 0, "key")
    else:
        name = _call_argument(node, 1, "name")
    if name is None:
        return True
    return not _different_constant_name(name)


def _is_structural_commentary(node: ast.AST) -> bool:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return _assignment_is_finding(node)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return _target_writes_doc(node.target)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return any(item.optional_vars is not None and _target_writes_doc(item.optional_vars) for item in node.items)
    if isinstance(node, ast.ExceptHandler):
        return bool(node.name) and (_sensitive_name(node.name) or node.name == "_")
    if isinstance(node, ast.Call):
        return _call_is_doc_write(node)
    return False


def _node_span(text: str, starts: list[int], node: ast.AST) -> tuple[int, int, str] | None:
    lineno = getattr(node, "lineno", None)
    col = getattr(node, "col_offset", None)
    if lineno is None or col is None:
        return None
    end_line = node.end_lineno if node.end_lineno is not None else lineno
    end_column = node.end_col_offset if node.end_col_offset is not None else col
    return (_position(starts, lineno, col), _position(starts, end_line, end_column), "")


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _alias_is_finding(node: ast.alias) -> bool:
    bound = node.asname or node.name.rsplit(".", 1)[-1]
    if _sensitive_name(node.asname) or node.asname == "_" or _sensitive_name(bound) or bound == "_":
        return True
    if node.name in _ALIAS_NAMES or node.asname in _ALIAS_NAMES:
        return True
    return False


def _called(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    return isinstance(parent, ast.Call) and parent.func is node


def _binding_node(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and _sensitive_name(node.id):
        return node
    if isinstance(node, ast.arg) and (_sensitive_name(node.arg) or node.arg == "_"):
        return node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
        _sensitive_name(node.name) or node.name == "_"
    ):
        return node
    if isinstance(node, ast.alias) and _alias_is_finding(node):
        return node
    if isinstance(node, (ast.Global, ast.Nonlocal)) and any(
        _sensitive_name(name) or name == "_" for name in node.names
    ):
        return node
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and _sensitive_name(node.name):
        return node
    if isinstance(node, ast.MatchMapping) and _sensitive_name(node.rest):
        return node
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _ALIAS_NAMES and not _called(node, parents):
        return node
    if (
        isinstance(node, ast.Attribute)
        and node.attr in _ALIAS_NAMES
        and isinstance(node.value, ast.Name)
        and node.value.id == "builtins"
        and not _called(node, parents)
    ):
        return node
    return None


def _doc_token(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "__doc__":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "__doc__":
        return True
    if isinstance(node, ast.keyword) and node.arg == "__doc__":
        return True
    if isinstance(node, ast.Constant) and node.value == "__doc__":
        return True
    if isinstance(node, ast.alias) and (node.asname == "__doc__" or node.name == "__doc__" or node.name.endswith(".__doc__")):
        return True
    if isinstance(node, ast.arg) and node.arg == "__doc__":
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "__doc__":
        return True
    if isinstance(node, ast.ExceptHandler) and node.name == "__doc__":
        return True
    if isinstance(node, (ast.Global, ast.Nonlocal)) and "__doc__" in node.names:
        return True
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == "__doc__":
        return True
    if isinstance(node, ast.MatchMapping) and node.rest == "__doc__":
        return True
    if isinstance(node, (ast.TypeVar, ast.TypeVarTuple, ast.ParamSpec)) and node.name == "__doc__":
        return True
    return False


def _constant_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant)


def _namespace_is_restricted(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    if not (_namespace_call(node) or _is_dunder_dict(node)):
        return False
    parent = parents.get(node)
    if (
        isinstance(parent, ast.Subscript)
        and parent.value is node
        and isinstance(parent.ctx, ast.Load)
        and _constant_key(parent.slice)
    ):
        return False
    return True


def _lookup_of_dict(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    matched = isinstance(func, ast.Name) and func.id in _LOOKUP_NAMES
    if not matched and isinstance(func, ast.Attribute) and func.attr in _LOOKUP_NAMES and _builtin_module(func.value):
        matched = True
    if not matched:
        return False
    candidates = list(node.args[1:2])
    for item in node.keywords:
        if item.arg in {None, "name"}:
            candidates.append(item.value)
    return any(_const_text(item) == "__dict__" for item in candidates)


def _banned_import(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "builtins" and alias.asname for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module in {"operator", "builtins"}:
        return any(alias.name in _REFLECT_IMPORTS or alias.asname in _REFLECT_IMPORTS for alias in node.names)
    return False


def _type_call_allowed(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else None
    if name is None or node.keywords:
        return False
    for item in TYPE_CALL_ALLOWLIST:
        if not item or item[0] != name or len(node.args) != len(item) - 1:
            continue
        matched = True
        for arg, want in zip(node.args, item[1:]):
            if isinstance(arg, ast.Name) and arg.id == want:
                continue
            if isinstance(arg, ast.Constant) and arg.value == want:
                continue
            matched = False
            break
        if matched:
            return True
    return False


def _three_arg_type(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    matched = isinstance(func, ast.Name) and func.id == "type"
    if not matched and isinstance(func, ast.Attribute) and func.attr == "type" and _builtin_module(func.value):
        matched = True
    if not matched:
        return False
    if len(node.args) < 3 and not any(item.arg == "dict" for item in node.keywords):
        return False
    return not _type_call_allowed(node)


def _literal_string_tree(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_literal_string_tree(item) for item in node.elts)
    return False


def _underscore_tuple(node: ast.AST) -> bool:
    if not isinstance(node, (ast.Tuple, ast.List)) or not node.elts:
        return False
    for item in node.elts:
        if isinstance(item, ast.Name) and item.id == "_":
            continue
        if isinstance(item, (ast.Tuple, ast.List)) and _underscore_tuple(item):
            continue
        return False
    return True


def _underscore_literal_target(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        target, iter_node = node.target, node.iter
    else:
        return None
    if not _underscore_tuple(target):
        return None
    if not isinstance(iter_node, (ast.List, ast.Tuple, ast.Set)) or not iter_node.elts:
        return None
    if not all(_literal_string_tree(item) for item in iter_node.elts):
        return None
    return target


def _spans_overlap(spans: list[tuple[int, int, str]], start: int, end: int) -> bool:
    for old_start, old_end, _replacement in spans:
        if start < old_end and end > old_start:
            return True
    return False


def _commentary_write_spans(
    text: str,
    starts: list[int],
    tree: ast.AST,
    *,
    ban_doc_token: bool,
) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        chosen: list[ast.AST] = []
        if _is_structural_commentary(node):
            chosen.append(node)
        bound = _binding_node(node, parents)
        if bound is not None and bound not in chosen:
            chosen.append(bound)
        if ban_doc_token and _doc_token(node) and node not in chosen:
            chosen.append(node)
        if isinstance(node, ast.Name) and node.id == "__builtins__" and node not in chosen:
            chosen.append(node)
        if _namespace_is_restricted(node, parents) and node not in chosen:
            chosen.append(node)
        if _lookup_of_dict(node) and node not in chosen:
            chosen.append(node)
        if _banned_import(node) and node not in chosen:
            chosen.append(node)
        if _three_arg_type(node) and node not in chosen:
            chosen.append(node)
        literal = _underscore_literal_target(node)
        if literal is not None and literal not in chosen:
            chosen.append(literal)
        for item in chosen:
            span = _node_span(text, starts, item)
            if span is None or _spans_overlap(spans, span[0], span[1]):
                continue
            spans.append(span)
    return spans


def _body_no_effect_spans(
    text: str,
    starts: list[int],
    body: list[ast.stmt],
    *,
    allow_empty: bool,
    stub_ok: bool,
) -> list[tuple[int, int, str]]:
    discarded = []
    for statement in body:
        if not isinstance(statement, ast.Expr):
            continue
        if stub_ok and len(body) == 1 and _bare_ellipsis(statement, text, starts):
            continue
        if _expression_is_allowed(statement.value):
            continue
        discarded.append(statement)
    if not discarded:
        return []
    replace_first = not allow_empty and len(discarded) == len(body)
    spans: list[tuple[int, int, str]] = []
    for index, statement in enumerate(discarded):
        end_line = statement.end_lineno if statement.end_lineno is not None else statement.lineno
        end_column = statement.end_col_offset if statement.end_col_offset is not None else statement.col_offset
        start = _position(starts, statement.lineno, statement.col_offset)
        end = _position(starts, end_line, end_column)
        if replace_first and index == 0:
            spans.append((start, end, "pass"))
            continue
        if _outside_is_whitespace(text, starts, start, end):
            start, end = _expand_lines(text, starts, start, end)
        spans.append((start, end, ""))
    return spans


def _apply_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    updated = text
    for start, end, replacement in sorted(spans, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def strip_python(text: str) -> str:
    style = _newline_style(text)
    normalized = _normalize_newlines(text)
    stripped = _apply_spans(normalized, python_spans(text))
    return _restore_newlines(stripped, style)


def _rejected_text(text: str) -> bool:
    if text.startswith("\ufeff"):
        return True
    for char in text:
        if char != "#" and unicodedata.normalize("NFKC", char) == "#":
            return True
    return False


def python_issue_lines(text: str, *, ban_doc_token: bool = True) -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    try:
        spans = python_spans(text, ban_doc_token=ban_doc_token)
    except (SyntaxError, ValueError, UnicodeError, tokenize.TokenError):
        return [(1, "unscanned")]
    found: list[tuple[int, str]] = []
    for start, _end, replacement in spans:
        kind = "docstring" if replacement == "pass" or _span_is_docstring(normalized, start) else "comment"
        found.append((_line_number(starts, start), kind))
    return _unique_lines(found)


def _span_is_docstring(text: str, start: int) -> bool:
    while start < len(text) and text[start] in " \t":
        start += 1
    return start < len(text) and text[start] in "\"'"


def _unique_lines(found: list[tuple[int, str]]) -> list[tuple[int, str]]:
    seen: set[tuple[int, str]] = set()
    ordered: list[tuple[int, str]] = []
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _scan_hash_config(text: str, markers: str = "#") -> tuple[list[tuple[int, int]], list[int]]:
    spans: list[tuple[int, int]] = []
    unclosed: list[int] = []
    index = 0
    size = len(text)
    while index < size:
        if text.startswith('"""', index) or text.startswith("'''", index):
            quote = text[index : index + 3]
            opened = index
            index += 3
            closed = False
            while index < size:
                if text.startswith(quote, index):
                    index += 3
                    closed = True
                    break
                if quote == '"""' and text[index] == "\\":
                    index += 2
                    continue
                index += 1
            if not closed:
                unclosed.append(opened)
            continue
        char = text[index]
        if char in "'\"":
            quote = char
            opened = index
            index += 1
            closed = False
            while index < size:
                if text[index] == "\n":
                    break
                if quote == '"' and text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                unclosed.append(opened)
            continue
        if char in markers:
            start = index
            while index < size and text[index] != "\n":
                index += 1
            spans.append((start, index))
            continue
        index += 1
    return spans, unclosed


def _hash_comment_spans(text: str, markers: str = "#") -> list[tuple[int, int]]:
    spans, _unclosed = _scan_hash_config(text, markers)
    return spans


def _config_issue_lines(text: str, markers: str = "#") -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    spans, unclosed = _scan_hash_config(normalized, markers)
    found = [(_line_number(starts, offset), "unscanned") for offset in unclosed]
    found.extend((_line_number(starts, start), "comment") for start, _end in spans)
    return _unique_lines(found)


def hash_config_issue_lines(text: str) -> list[tuple[int, str]]:
    return _config_issue_lines(text)


def gitignore_issue_lines(text: str) -> list[tuple[int, str]]:
    found = []
    for line_number, line in enumerate(_normalize_newlines(text).split("\n"), 1):
        stripped = line.lstrip(" \t")
        if stripped.startswith("#"):
            found.append((line_number, "comment"))
    return found


def _inno_section_name(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) < 3 or stripped[0] != "[":
        return None
    end = stripped.find("]")
    if end < 2:
        return None
    return stripped[1:end].strip()


def _scan_inno_code_line(line: str, line_number: int, brace_open: int | None, paren_open: int | None) -> tuple[list[tuple[int, str]], int | None, int | None]:
    found: list[tuple[int, str]] = []
    quote = ""
    index = 0
    if brace_open is not None:
        end = line.find("}")
        if end < 0:
            return found, brace_open, paren_open
        brace_open = None
        index = end + 1
    elif paren_open is not None:
        end = line.find("*)")
        if end < 0:
            return found, brace_open, paren_open
        paren_open = None
        index = end + 2
    size = len(line)
    while index < size:
        if quote == "'":
            if line.startswith("''", index):
                index += 2
                continue
            if line[index] == "'":
                quote = ""
            index += 1
            continue
        if quote == '"':
            if line[index] == '"':
                quote = ""
            index += 1
            continue
        if line.startswith("//", index):
            found.append((line_number, "comment"))
            break
        if line.startswith("(*", index):
            found.append((line_number, "comment"))
            end = line.find("*)", index + 2)
            if end < 0:
                return found, brace_open, line_number
            index = end + 2
            continue
        if line[index] == "{":
            if index + 1 < size and line[index + 1] == "#":
                end = line.find("}", index + 2)
                if end < 0:
                    found.append((line_number, "unscanned"))
                    break
                index = end + 1
                continue
            found.append((line_number, "comment"))
            end = line.find("}", index + 1)
            if end < 0:
                return found, line_number, paren_open
            index = end + 1
            continue
        if line[index] in "'\"":
            quote = line[index]
            index += 1
            continue
        index += 1
    if quote:
        found.append((line_number, "unscanned"))
    return found, brace_open, paren_open


def inno_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    found: list[tuple[int, str]] = []
    in_code = False
    brace_open: int | None = None
    paren_open: int | None = None
    lines = normalized.split("\n")
    for line_number, line in enumerate(lines, 1):
        section = _inno_section_name(line)
        if section is not None:
            if brace_open is not None:
                found.append((brace_open, "unscanned"))
                brace_open = None
            if paren_open is not None:
                found.append((paren_open, "unscanned"))
                paren_open = None
            in_code = section.casefold() == "code"
        if in_code:
            extra, brace_open, paren_open = _scan_inno_code_line(line, line_number, brace_open, paren_open)
            found.extend(extra)
            continue
        stripped = line.lstrip(" \t")
        if stripped.startswith(";"):
            found.append((line_number, "comment"))
            continue
        mark = _inno_slash_comment(line)
        if mark == -1:
            found.append((line_number, "unscanned"))
        elif mark is not None:
            found.append((line_number, "comment"))
    if brace_open is not None:
        found.append((brace_open, "unscanned"))
    if paren_open is not None:
        found.append((paren_open, "unscanned"))
    return _unique_lines(found)


def _inno_slash_comment(line: str) -> int | None:
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            if line.startswith("''", index):
                index += 2
                continue
            if char == "'":
                quote = ""
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if line.startswith("//", index):
            return index
        index += 1
    if quote:
        return -1
    return None


class _YamlScalar:
    def __init__(self, text: str, line: int, line_map: list[int]) -> None:
        self.text = text
        self.line = line
        self.line_map = line_map


class _YamlMap:
    def __init__(self, entries: list[tuple[str, int, object]]) -> None:
        self.entries = entries

    def get(self, key: str) -> object:
        for name, _line, value in self.entries:
            if name == key:
                return value
        return None


class _YamlSeq:
    def __init__(self, items: list[object]) -> None:
        self.items = items


class _YamlDocument:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.index = 0
        self.findings: list[tuple[int, str]] = []
        self.root: object = None
        self.failed = False

    def parse(self) -> None:
        self.root = self._parse_block(0)
        self._skip_noise()
        if self.index < len(self.lines):
            self._fail(self.index + 1)

    def _fail(self, line: int) -> None:
        self.failed = True
        self.findings.append((line, "unscanned"))

    def _skip_noise(self) -> None:
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            if raw.strip() == "":
                self.index += 1
                continue
            if raw.startswith("\t") or (raw[:1] == " " and "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]):
                self._fail(self.index + 1)
                self.index += 1
                continue
            code, comment = _split_yaml_comment(raw)
            if comment is not None and code.strip() == "":
                self.findings.append((self.index + 1, "comment"))
                self.index += 1
                continue
            break

    def _peek(self) -> tuple[int, str, int] | None:
        self._skip_noise()
        if self.index >= len(self.lines) or self.failed:
            return None
        raw = self.lines[self.index]
        indent = len(raw) - len(raw.lstrip(" "))
        code, comment = _split_yaml_comment(raw)
        if comment is not None and code.strip() != "":
            self.findings.append((self.index + 1, "comment"))
        return indent, code.strip(), self.index

    def _parse_block(self, min_indent: int) -> object:
        peeked = self._peek()
        if peeked is None or peeked[0] < min_indent or self.failed:
            return None
        if peeked[1] == "-" or peeked[1].startswith("- "):
            return self._parse_seq(peeked[0])
        return self._parse_map(peeked[0])

    def _parse_map(self, indent: int) -> _YamlMap:
        entries: list[tuple[str, int, object]] = []
        while not self.failed:
            peeked = self._peek()
            if peeked is None or peeked[0] != indent:
                break
            _indent, code, line_index = peeked
            if code == "-" or code.startswith("- "):
                break
            key, value = _mapping_pieces(code)
            if key is None:
                self._fail(line_index + 1)
                self.index += 1
                break
            self.index += 1
            parsed = self._parse_value(indent, value, line_index)
            entries.append((key, line_index + 1, parsed))
        return _YamlMap(entries)

    def _parse_seq(self, indent: int) -> _YamlSeq:
        items: list[object] = []
        while not self.failed:
            peeked = self._peek()
            if peeked is None or peeked[0] != indent:
                break
            _indent, code, line_index = peeked
            if code != "-" and not code.startswith("- "):
                break
            rest = "" if code == "-" else code[2:].strip() if code.startswith("- ") else code[1:].strip()
            if code != "-" and not code.startswith("- "):
                self._fail(line_index + 1)
                self.index += 1
                break
            self.index += 1
            if rest == "":
                items.append(self._parse_block(indent + 1))
                continue
            key, value = _mapping_pieces(rest)
            if key is None:
                if _BLOCK_STYLE.fullmatch(rest):
                    items.append(self._parse_block_scalar(indent, rest, line_index))
                    continue
                if _starts_with_indicator(rest):
                    self._fail(line_index + 1)
                items.append(_plain_scalar(rest, line_index + 1))
                continue
            entries = [(key, line_index + 1, self._parse_value(indent + 2, value, line_index))]
            entries.extend(self._following_map_entries(indent))
            items.append(_YamlMap(entries))
        return _YamlSeq(items)

    def _following_map_entries(self, dash_indent: int) -> list[tuple[str, int, object]]:
        entries: list[tuple[str, int, object]] = []
        while not self.failed:
            peeked = self._peek()
            if peeked is None or peeked[0] <= dash_indent:
                break
            indent, code, line_index = peeked
            if code == "-" or code.startswith("- "):
                break
            key, value = _mapping_pieces(code)
            if key is None:
                break
            self.index += 1
            entries.append((key, line_index + 1, self._parse_value(indent, value, line_index)))
        return entries

    def _parse_value(self, header_indent: int, value: str, line_index: int) -> object:
        stripped = value.strip()
        if stripped == "":
            return self._parse_block(header_indent + 1)
        if _BLOCK_STYLE.fullmatch(stripped):
            return self._parse_block_scalar(header_indent, stripped, line_index)
        if _starts_with_indicator(stripped):
            self._fail(line_index + 1)
            return _plain_scalar("", line_index + 1)
        if stripped[0] in "'\"" and not _quotes_closed(stripped):
            self._fail(line_index + 1)
            return _plain_scalar(stripped, line_index + 1)
        return _plain_scalar(stripped, line_index + 1)

    def _parse_block_scalar(self, header_indent: int, style: str, line_index: int) -> _YamlScalar:
        digit = "".join(char for char in style if char.isdigit())
        content_indent = header_indent + int(digit) if digit else None
        pieces: list[tuple[int, str]] = []
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            if raw.strip() == "":
                pieces.append((self.index + 1, ""))
                self.index += 1
                continue
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                self._fail(self.index + 1)
                break
            indent = len(raw) - len(raw.lstrip(" "))
            if content_indent is None:
                if indent <= header_indent:
                    break
                content_indent = indent
            if indent < content_indent:
                break
            if len(raw) < content_indent or raw[:content_indent].strip() != "":
                self._fail(self.index + 1)
                self.index += 1
                continue
            pieces.append((self.index + 1, raw[content_indent:]))
            self.index += 1
        text = "\n".join(piece for _line, piece in pieces)
        if pieces:
            text += "\n"
        line_map = [line for line, _piece in pieces] or [line_index + 1]
        return _YamlScalar(text, line_index + 1, line_map)


def _mapping_pieces(code: str) -> tuple[str | None, str]:
    matched = _KEY_LINE.match(code)
    if matched is None:
        return None, code
    return matched.group(1), matched.group(2).strip()


def _starts_with_indicator(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped[0] in "&*!%{}" or stripped.startswith("<<"):
        return True
    return stripped[0] == "[" and "{" in stripped


def _quotes_closed(value: str) -> bool:
    if len(value) < 2 or value[0] not in "'\"":
        return True
    quote = value[0]
    index = 1
    while index < len(value):
        if quote == "'" and value.startswith("''", index):
            index += 2
            continue
        if quote == '"' and value[index] == "\\":
            index += 2
            continue
        if value[index] == quote:
            return index == len(value) - 1
        index += 1
    return False


def _plain_scalar(value: str, line: int) -> _YamlScalar:
    text = value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" and _quotes_closed(value):
        text = value[1:-1]
    return _YamlScalar(text, line, [line])


def _split_yaml_comment(line: str) -> tuple[str, str | None]:
    single = False
    double = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "'" and not double:
            if single and index + 1 < len(line) and line[index + 1] == "'":
                index += 2
                continue
            single = not single
        elif char == '"' and not single:
            if double and index > 0 and line[index - 1] == "\\":
                index += 1
                continue
            double = not double
        elif char == "#" and not single and not double:
            return line[:index], line[index:]
        index += 1
    return line, None


_RUN_TOKEN = re.compile(r"[-A-Za-z0-9_./:=,@+,]+")
_ALLOWED_SHELLS = {"bash", "pwsh", "cmd"}
_COMMANDS = set(COMMAND_ALLOWLIST)


def _split_run_commands(line: str) -> list[list[str]] | None:
    commands: list[list[str]] = []
    tokens: list[str] = []
    index = 0
    size = len(line)
    while index < size:
        if line[index] in " \t":
            index += 1
            continue
        if line.startswith("&&", index):
            if not tokens:
                return None
            commands.append(tokens)
            tokens = []
            index += 2
            continue
        if line[index] == "|":
            if not tokens:
                return None
            commands.append(tokens)
            tokens = []
            index += 1
            continue
        if line[index] in "'\"":
            quote = line[index]
            index += 1
            start = index
            while index < size and line[index] != quote:
                if line[index] == "\\" or line[index] == "#":
                    return None
                index += 1
            if index >= size:
                return None
            tokens.append(line[start:index])
            index += 1
            continue
        matched = _RUN_TOKEN.match(line, index)
        if matched is None:
            return None
        tokens.append(matched.group(0))
        index = matched.end()
    if not tokens:
        return None if commands else []
    commands.append(tokens)
    return commands


def _base_name(token: str) -> str:
    return token.casefold().replace("\\", "/").rsplit("/", 1)[-1]


def _command_allowed(tokens: list[str]) -> bool:
    if not tokens:
        return False
    subject = tokens
    if _base_name(tokens[0]) == "sudo":
        if len(tokens) < 2 or _base_name(tokens[1]) == "sudo":
            return False
        subject = tokens[1:]
    if not subject or _base_name(subject[0]) not in EXECUTABLE_ALLOWLIST or _base_name(subject[0]) == "sudo":
        return False
    return tuple(subject) in _COMMANDS


def _run_line_allowed(line: str) -> bool:
    if line.strip() == "":
        return True
    if any(mark in line for mark in ("#", "$(", "`", "<<", "${")):
        return False
    commands = _split_run_commands(line)
    if commands is None:
        return False
    return all(_command_allowed(tokens) for tokens in commands)


def _drop_unquoted_hash(line: str) -> str:
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "#":
            return line[:index]
        index += 1
    return line


def _check_run(scalar: _YamlScalar, findings: list[tuple[int, str]]) -> None:
    lines = scalar.text.split("\n")
    for offset, line in enumerate(lines):
        if line.strip() == "" or _run_line_allowed(line):
            continue
        source = scalar.line_map[offset] if offset < len(scalar.line_map) else scalar.line
        if "#" in line:
            findings.append((source, "comment"))
        if "#" not in line or not _run_line_allowed(_drop_unquoted_hash(line)):
            findings.append((source, "inline-python"))


def _child_context(context: str, key: str) -> str | None:
    if context == "jobs":
        return "job"
    allowed = KNOWN_KEYS.get(context)
    if allowed is None or key not in allowed:
        return None
    return _KEY_NEST.get((context, key), "scalar")


def _check_leading_hash(scalar: _YamlScalar, findings: list[tuple[int, str]]) -> None:
    if "\n" not in scalar.text:
        return
    lines = scalar.text.split("\n")
    for offset, line in enumerate(lines):
        if not line.lstrip(" \t").startswith("#"):
            continue
        source = scalar.line_map[offset] if offset < len(scalar.line_map) else scalar.line
        findings.append((source, "comment"))


def _visit_yaml(node: object, findings: list[tuple[int, str]], context: str) -> None:
    if node is None:
        return
    if isinstance(node, _YamlScalar):
        if context != "rejected":
            _check_leading_hash(node, findings)
        return
    if isinstance(node, _YamlSeq):
        if context == "steps":
            item_context = "step"
        elif context == "rejected":
            item_context = "rejected"
        else:
            item_context = "scalar"
        for item in node.items:
            _visit_yaml(item, findings, item_context)
        return
    if not isinstance(node, _YamlMap):
        findings.append((1, "unscanned"))
        return
    seen: set[str] = set()
    for key, line, child in node.entries:
        if key in seen:
            findings.append((line, "unscanned"))
        seen.add(key)
        child_context = _child_context(context, key)
        if child_context is None:
            findings.append((line, "unscanned"))
            _visit_yaml(child, findings, "rejected")
            continue
        if child_context == "shell":
            if not isinstance(child, _YamlScalar) or child.text.strip().casefold() not in _ALLOWED_SHELLS:
                findings.append((child.line if isinstance(child, _YamlScalar) else line, "inline-python"))
            continue
        if child_context == "run":
            if not isinstance(child, _YamlScalar):
                findings.append((line, "unscanned"))
            else:
                _check_run(child, findings)
                _check_leading_hash(child, findings)
            continue
        if child_context == "job" and not isinstance(child, _YamlMap):
            findings.append((line, "unscanned"))
            continue
        _visit_yaml(child, findings, child_context)


def yaml_issue_lines(text: str) -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]
    normalized = _normalize_newlines(text)
    document = _YamlDocument(normalized.split("\n"))
    document.parse()
    if isinstance(document.root, _YamlSeq):
        _visit_yaml(document.root, document.findings, "steps")
    elif isinstance(document.root, _YamlMap):
        _visit_yaml(document.root, document.findings, "root")
    elif document.root is not None:
        document.findings.append((1, "unscanned"))
    return _unique_lines(document.findings)


def ini_issue_lines(text: str) -> list[tuple[int, str]]:
    return _config_issue_lines(text, markers="#;")

def html_issue_lines(text: str) -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = [(_line_number(starts, match.start()), "unscanned") for match in _HTML_ACTIVE.finditer(normalized)]
    index = 0
    marker = "<!--"
    while True:
        start = normalized.find(marker, index)
        if start < 0:
            break
        found.append((_line_number(starts, start), "comment"))
        end = normalized.find("-->", start + len(marker))
        if end < 0:
            found.append((_line_number(starts, start), "unscanned"))
            break
        index = end + 3
    return _unique_lines(found)


def _json_executes_strings(node: object) -> bool:
    if isinstance(node, dict):
        if any(isinstance(key, str) and key.casefold() == "scripts" for key in node):
            return True
        return any(_json_executes_strings(value) for value in node.values())
    if isinstance(node, list):
        return any(_json_executes_strings(value) for value in node)
    return False


def json_issue_lines(text: str, path: str = "") -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]
    name = _normalized_path(path).rsplit("/", 1)[-1]

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        document: dict[str, object] = {}
        for key, value in items:
            if key in seen:
                raise ValueError(key)
            seen.add(key)
            document[key] = value
        return document

    try:
        loaded = json.loads(text, object_pairs_hook=pairs)
    except (json.JSONDecodeError, ValueError, UnicodeError):
        return [(1, "unscanned")]
    if name == "package.json" or _json_executes_strings(loaded):
        return [(1, "unscanned")]
    return []


def hash_manifest_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    found: list[tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        if "#" in line:
            found.append((number, "comment"))
            continue
        if HASH_MANIFEST_LINE.fullmatch(line):
            continue
        found.append((number, "unscanned"))
    return found


def _requirement_text(name: str) -> bool:
    folded = name.casefold()
    return folded.endswith(".txt") and ("requirement" in folded or "constraint" in folded)


def _normalized_path(path: str) -> str:
    return path.replace("\\", "/").casefold()


def _kind(path: str) -> str | None:
    normalized = _normalized_path(path)
    name = normalized.rsplit("/", 1)[-1]
    suffix = Path(normalized).suffix
    if suffix in _PY_SUFFIXES:
        return "python"
    if suffix == ".iss":
        return "inno"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix in {".ini", ".cfg"}:
        return "ini"
    if suffix in _HASH_SUFFIXES:
        return "hashconfig"
    if name in {".gitignore", ".gitattributes"}:
        return "gitignore"
    if suffix == ".json":
        return "json"
    if suffix in {".html", ".htm", ".svg", ".xml"}:
        return "html"
    if _requirement_text(name):
        return "hashconfig"
    if name in _HASH_MANIFEST_NAMES:
        return "manifest"
    if suffix == ".txt" and name in _PROSE_TEXT_NAMES:
        return None
    if suffix in _DATA_SUFFIXES:
        return None
    return "unscanned"


def _issue_lines(kind: str, text: str, path: str = "") -> list[tuple[int, str]]:
    if kind == "python":
        return python_issue_lines(text, ban_doc_token=path not in DOC_TOKEN_ALLOWLIST)
    if kind == "inno":
        return inno_issue_lines(text)
    if kind == "yaml":
        return yaml_issue_lines(text)
    if kind == "hashconfig":
        return hash_config_issue_lines(text)
    if kind == "ini":
        return ini_issue_lines(text)
    if kind == "gitignore":
        return gitignore_issue_lines(text)
    if kind == "html":
        return html_issue_lines(text)
    if kind == "json":
        return json_issue_lines(text, path)
    if kind == "manifest":
        return hash_manifest_issue_lines(text)
    return [(1, "unscanned")]


def strip_text(kind: str, text: str) -> str:
    if kind == "python":
        return strip_python(text)
    style = _newline_style(text)
    normalized = _normalize_newlines(text)
    if kind == "yaml":
        stripped = _strip_yaml(normalized)
    elif kind == "hashconfig":
        stripped = _delete_spans(normalized, _hash_comment_spans(normalized))
    elif kind == "ini":
        stripped = _delete_spans(normalized, _hash_comment_spans(normalized, markers="#;"))
    elif kind == "gitignore":
        kept = [line for line in normalized.split("\n") if not line.lstrip(" \t").startswith("#")]
        stripped = "\n".join(kept)
        if normalized.endswith("\n") and not stripped.endswith("\n"):
            stripped += "\n"
    elif kind == "inno":
        stripped = _strip_inno(normalized)
    elif kind == "html":
        stripped = normalized
        while "<!--" in stripped:
            start = stripped.find("<!--")
            end = stripped.find("-->", start + 4)
            end = len(stripped) if end < 0 else end + 3
            stripped = stripped[:start] + stripped[end:]
    else:
        stripped = normalized
    return _restore_newlines(stripped, style)


def _delete_spans(text: str, spans: list[tuple[int, int]]) -> str:
    starts = _line_starts(text)
    expanded: list[tuple[int, int, str]] = []
    for start, end in spans:
        line_begin, _content_end, _full_end = _line_bounds(text, starts, _line_number(starts, start))
        while start > line_begin and text[start - 1] in " \t":
            start -= 1
        if _outside_is_whitespace(text, starts, start, end):
            start, end = _expand_lines(text, starts, start, end)
        expanded.append((start, end, ""))
    return _apply_spans(text, expanded)


def _strip_yaml(text: str) -> str:
    lines = text.split("\n")
    comments = {line for line, kind in yaml_issue_lines(text) if kind == "comment"}
    kept = []
    for number, line in enumerate(lines, 1):
        if number not in comments:
            kept.append(line)
            continue
        code, _comment = _split_yaml_comment(line)
        if code.strip() == "":
            continue
        kept.append(code.rstrip())
    stripped = "\n".join(kept)
    if text.endswith("\n") and not stripped.endswith("\n"):
        stripped += "\n"
    return stripped


def _strip_inno(text: str) -> str:
    kept = []
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        if stripped.startswith(";"):
            continue
        mark = _inno_slash_comment(line)
        if mark is None:
            kept.append(line)
            continue
        kept.append(line[:mark].rstrip())
    stripped = "\n".join(kept)
    if text.endswith("\n") and not stripped.endswith("\n"):
        stripped += "\n"
    return stripped


def is_documentation(path: str) -> bool:
    normalized = _normalized_path(path)
    suffix = Path(normalized).suffix
    if suffix == ".md":
        return True
    if normalized.startswith("docs/") and suffix in _DOC_IMAGE_SUFFIXES:
        return True
    return False


def tracked_paths(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    raw = completed.stdout.decode("utf-8")
    return [item for item in raw.split("\0") if item]


def issues_for(path: str, text: str) -> list[Finding]:
    if path in FILE_ALLOWLIST or is_documentation(path):
        return []
    kind = _kind(path)
    if kind is None:
        return []
    if kind == "unscanned":
        return [Finding(path, 1, "unscanned")]
    if _rejected_text(text):
        return [Finding(path, 1, "unscanned")]
    try:
        parsed = _issue_lines(kind, text, path)
    except Exception:
        return [Finding(path, 1, "unscanned")]
    findings: list[Finding] = []
    for line, issue in parsed:
        if issue == "comment" and _directive_allowed(path, _line_text(text, line)):
            continue
        findings.append(Finding(path, line, issue))
    return findings


def _read_tracked(path: Path) -> tuple[str | None, bool]:
    try:
        if path.is_symlink() and not path.exists():
            return None, False
        data = path.read_bytes()
    except OSError:
        return None, False
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return None, False
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        return None, False


def scan(root: Path | None = None) -> list[Finding]:
    base = root or repo_root()
    findings: list[Finding] = []
    for relative in tracked_paths(base):
        if relative in FILE_ALLOWLIST or is_documentation(relative):
            continue
        kind = _kind(relative)
        if kind is None:
            continue
        if kind == "unscanned":
            findings.append(Finding(relative, 1, "unscanned"))
            continue
        text, ok = _read_tracked(base / relative)
        if not ok or text is None:
            findings.append(Finding(relative, 1, "unscanned"))
            continue
        findings.extend(issues_for(relative, text))
    return findings


def _line_text(text: str, line: int) -> str:
    return _normalize_newlines(text).split("\n")[line - 1]
