from __future__ import annotations

import ast
import re
import subprocess
import tokenize
from io import StringIO
from pathlib import Path

FILE_ALLOWLIST = (
    "src/backtester/costs.py",
    "src/backtester/costs_desk.py",
)

DIRECTIVE_ALLOWLIST: tuple[tuple[str, str], ...] = ()

_HASH_SUFFIXES = {".toml"}
_SLASH_SUFFIXES = {".js", ".mjs", ".cjs", ".qml"}
_PY_SUFFIXES = {".py", ".pyw", ".spec"}
_DOC_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
_DATA_SUFFIXES = {".csv", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".json", ".png", ".txt", ".webp", ".xz", ".zip"}
_PY_EXES = {"python", "python2", "python3", "py", "python.exe", "python2.exe", "python3.exe", "py.exe"}
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


def _expression_has_effect(node: ast.AST) -> bool:
    if isinstance(node, ast.Lambda):
        defaults = list(node.args.defaults)
        defaults.extend(item for item in node.args.kw_defaults if item is not None)
        return any(_expression_has_effect(item) for item in defaults)
    if isinstance(node, ast.GeneratorExp):
        return bool(node.generators) and _expression_has_effect(node.generators[0].iter)
    if isinstance(node, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)):
        return True
    return any(_expression_has_effect(child) for child in ast.iter_child_nodes(node))


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


def python_spans(text: str) -> list[tuple[int, int, str]]:
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
        if _expression_has_effect(statement.value):
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


def python_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found: list[tuple[int, str]] = []
    for start, _end, replacement in python_spans(text):
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


def _hash_comment_spans(text: str, markers: str = "#") -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    size = len(text)
    while index < size:
        if text.startswith('"""', index) or text.startswith("'''", index):
            quote = text[index : index + 3]
            index += 3
            while index < size and not text.startswith(quote, index):
                if quote == '"""' and text[index] == "\\":
                    index += 2
                    continue
                index += 1
            index = min(size, index + 3)
            continue
        char = text[index]
        if char in "'\"":
            quote = char
            index += 1
            while index < size:
                if quote == '"' and text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char in markers:
            start = index
            while index < size and text[index] != "\n":
                index += 1
            spans.append((start, index))
            continue
        index += 1
    return spans


def hash_config_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = [(_line_number(starts, start), "comment") for start, _end in _hash_comment_spans(normalized)]
    return found


def gitignore_issue_lines(text: str) -> list[tuple[int, str]]:
    found = []
    for line_number, line in enumerate(_normalize_newlines(text).split("\n"), 1):
        stripped = line.lstrip(" \t")
        if stripped.startswith("#"):
            found.append((line_number, "comment"))
    return found


def powershell_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = [(_line_number(starts, start), "comment") for start, _end in _powershell_spans(normalized)]
    return found


def _here_string_opens(text: str, index: int) -> bool:
    cursor = index + 2
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    return cursor >= len(text) or text[cursor] == "\n"


def _skip_here_string(text: str, index: int, quote: str) -> int:
    newline = text.find("\n", index)
    if newline < 0:
        return len(text)
    cursor = newline + 1
    closer = quote + "@"
    while cursor < len(text):
        newline = text.find("\n", cursor)
        if newline < 0:
            newline = len(text)
        stripped = text[cursor:newline].lstrip(" \t")
        if stripped.startswith(closer):
            return newline
        cursor = newline + 1 if newline < len(text) else len(text)
    return len(text)


def _powershell_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    size = len(text)
    while index < size:
        if text.startswith("<#", index):
            start = index
            index += 2
            while index < size and not text.startswith("#>", index):
                index += 1
            index = min(size, index + 2)
            spans.append((start, index))
            continue
        if text.startswith('@"', index) and _here_string_opens(text, index):
            index = _skip_here_string(text, index, '"')
            continue
        if text.startswith("@'", index) and _here_string_opens(text, index):
            index = _skip_here_string(text, index, "'")
            continue
        char = text[index]
        if char == "'":
            index += 1
            while index < size:
                if text.startswith("''", index):
                    index += 2
                    continue
                if text[index] == "'":
                    index += 1
                    break
                index += 1
            continue
        if char == '"':
            index += 1
            while index < size:
                if text[index] == "`":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if char == "#":
            start = index
            while index < size and text[index] != "\n":
                index += 1
            spans.append((start, index))
            continue
        index += 1
    return spans


def batch_issue_lines(text: str) -> list[tuple[int, str]]:
    found = []
    for line_number, line in enumerate(_normalize_newlines(text).split("\n"), 1):
        stripped = line.lstrip(" \t")
        folded = stripped.upper()
        if folded == "REM" or folded.startswith("REM ") or folded.startswith("REM\t") or stripped.startswith("::"):
            found.append((line_number, "comment"))
    return found


def inno_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    found = []
    for line_number, line in enumerate(normalized.split("\n"), 1):
        stripped = line.lstrip(" \t")
        if stripped.startswith(";"):
            found.append((line_number, "comment"))
            continue
        if _inno_slash_comment(line) is not None:
            found.append((line_number, "comment"))
    return found


def _inno_slash_comment(line: str) -> int | None:
    quotes = False
    index = 0
    while index < len(line) - 1:
        char = line[index]
        if char == "'":
            if quotes and index + 1 < len(line) and line[index + 1] == "'":
                index += 2
                continue
            quotes = not quotes
            index += 1
            continue
        if char == '"':
            end = line.find('"', index + 1)
            if end < 0:
                return None
            index = end + 1
            continue
        if not quotes and line.startswith("//", index):
            return index
        index += 1
    return None


def shell_issue_lines(text: str) -> list[tuple[int, str]]:
    return _bash_issue_lines(_normalize_newlines(text))


def _shell_comment_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for kind, start, end in _bash_events(text):
        if kind == "comment":
            spans.append((start, end))
    return spans


def _exe_name(word: str) -> str:
    token = word.strip("\"'")
    return token.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _is_python_exe(word: str) -> bool:
    return _exe_name(word) in _PY_EXES


def _is_python_c_flag(word: str) -> bool:
    if word.startswith("--") or not word.startswith("-") or len(word) < 2:
        return False
    return "c" in word[1:]


def _command_is_inline_python(words: list[str], heredoc: bool) -> bool:
    if heredoc and any(_exe_name(word).endswith((".py", ".pyw")) for word in words):
        return True
    indexes = [index for index, word in enumerate(words) if _is_python_exe(word)]
    if not indexes:
        return False
    rest = words[indexes[0] + 1 :]
    if heredoc:
        return True
    return any(word == "-" or _is_python_c_flag(word) for word in rest)


def _is_python_shell(shell: str) -> bool:
    parts = shell.strip().split()
    if not parts:
        return False
    return _is_python_exe(parts[0].strip("\"'"))


def _is_powershell_shell(shell: str) -> bool:
    parts = shell.strip().split()
    if not parts:
        return False
    return _exe_name(parts[0].strip("\"'")) in {"pwsh", "powershell", "pwsh.exe", "powershell.exe"}


def _bash_issue_lines(text: str) -> list[tuple[int, str]]:
    starts = _line_starts(text)
    found = []
    for kind, start, end in _bash_events(text):
        if kind == "comment":
            found.append((_line_number(starts, start), "comment"))
        else:
            found.append((start, kind))
    return _unique_lines(found)


def _bash_events(text: str) -> list[tuple[str, int, int]]:
    events: list[tuple[str, int, int]] = []
    size = len(text)
    index = 0
    words: list[str] = []
    heredoc = False
    command_line = 1
    pending: list[tuple[str, bool]] = []

    def line_of(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    def flush() -> None:
        nonlocal words, heredoc
        if words and _command_is_inline_python(words, heredoc):
            events.append(("inline-python", command_line, command_line))
        words = []
        heredoc = False

    def consume_heredoc_body(delim: str, strip_tabs: bool) -> bool:
        nonlocal index
        while index < size:
            end = text.find("\n", index)
            if end < 0:
                end = size
            line = text[index:end]
            compared = line.lstrip("\t") if strip_tabs else line
            index = end + 1 if end < size else end
            if compared == delim:
                return True
        return False

    while index < size:
        if text.startswith("\\\n", index) or text.startswith("\\\r\n", index):
            index += 2 if text.startswith("\\\n", index) else 3
            continue
        char = text[index]
        if char in " \t":
            index += 1
            continue
        if char == "\n":
            flush()
            index += 1
            while pending:
                delim, strip_tabs = pending.pop(0)
                if not consume_heredoc_body(delim, strip_tabs):
                    events.append(("unscanned", line_of(max(0, index - 1)), 0))
                    pending.clear()
                    break
            command_line = line_of(index) if index < size else command_line
            continue
        if char == "#":
            start = index
            while index < size and text[index] != "\n":
                index += 1
            events.append(("comment", start, index))
            continue
        if text.startswith("<<<", index):
            heredoc = True
            if not words:
                command_line = line_of(index)
            index += 3
            continue
        if text.startswith("<<", index):
            if not words:
                command_line = line_of(index)
            heredoc = True
            cursor = index + 2
            strip_tabs = False
            if cursor < size and text[cursor] == "-":
                strip_tabs = True
                cursor += 1
            while cursor < size and text[cursor] in " \t":
                cursor += 1
            if cursor >= size:
                events.append(("unscanned", line_of(index), 0))
                return events
            if text[cursor] in "'\"":
                quote = text[cursor]
                cursor += 1
                end = text.find(quote, cursor)
                if end < 0:
                    events.append(("unscanned", line_of(index), 0))
                    return events
                delim = text[cursor:end]
                cursor = end + 1
            else:
                end = cursor
                while end < size and text[end] not in " \t\n|&;()<>":
                    end += 1
                delim = text[cursor:end]
                cursor = end
            if delim == "":
                events.append(("unscanned", line_of(index), 0))
                return events
            pending.append((delim, strip_tabs))
            index = cursor
            continue
        if text.startswith(">>", index) or text.startswith(">&", index) or text.startswith("<&", index):
            index += 2
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            flush()
            index += 2
            command_line = line_of(index)
            continue
        if char in "|&;":
            flush()
            index += 1
            command_line = line_of(index)
            continue
        if char in "<>":
            index += 1
            continue
        if char in "()":
            index += 1
            continue
        if char in "'\"":
            if not words:
                command_line = line_of(index)
            quote = char
            start = index
            index += 1
            closed = False
            while index < size:
                if quote == '"' and text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    closed = True
                    break
                index += 1
            words.append(text[start + 1 : index - 1] if closed else "quoted")
            if not closed:
                events.append(("unscanned", line_of(index - 1), 0))
                return events
            continue
        if text.startswith("$(", index):
            if not words:
                command_line = line_of(index)
            index += 2
            depth = 1
            while index < size and depth:
                if text[index] in "'\"":
                    quote = text[index]
                    index += 1
                    while index < size and text[index] != quote:
                        if text[index] == "\\":
                            index += 2
                            continue
                        index += 1
                    index = min(size, index + 1)
                    continue
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                index += 1
            words.append("substitution")
            if depth:
                events.append(("unscanned", line_of(max(0, index - 1)), 0))
                return events
            continue
        if char == "`":
            events.append(("unscanned", line_of(index), 0))
            return events
        if char == "\\":
            if not words:
                command_line = line_of(index)
            index += 2 if index + 1 < size else 1
            continue
        if not words:
            command_line = line_of(index)
        start = index
        while index < size and text[index] not in " \t\n|&;()<>#'\"`\\":
            index += 1
        words.append(text[start:index])
    flush()
    if pending:
        events.append(("unscanned", line_of(max(0, size - 1)), 0))
    return events


def _powershell_inline_lines(text: str) -> list[int]:
    masked = list(text)
    index = 0
    size = len(text)

    def blank(start: int, end: int) -> None:
        for cursor in range(start, end):
            if masked[cursor] != "\n":
                masked[cursor] = " "

    while index < size:
        if text.startswith("<#", index):
            start = index
            index += 2
            while index < size and not text.startswith("#>", index):
                index += 1
            index = min(size, index + 2)
            blank(start, index)
            continue
        if text.startswith('@"', index) and _here_string_opens(text, index):
            start = index
            index = _skip_here_string(text, index, '"')
            blank(start, index)
            continue
        if text.startswith("@'", index) and _here_string_opens(text, index):
            start = index
            index = _skip_here_string(text, index, "'")
            blank(start, index)
            continue
        char = text[index]
        if char == "'":
            start = index
            index += 1
            while index < size:
                if text.startswith("''", index):
                    index += 2
                    continue
                if text[index] == "'":
                    index += 1
                    break
                index += 1
            blank(start, index)
            continue
        if char == '"':
            start = index
            index += 1
            while index < size:
                if text[index] == "`":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            blank(start, index)
            continue
        if char == "#":
            start = index
            while index < size and text[index] != "\n":
                index += 1
            blank(start, index)
            continue
        index += 1
    found = []
    masked_text = "".join(masked)
    for line_number, line in enumerate(masked_text.split("\n"), 1):
        for segment in line.replace("|", ";").split(";"):
            words = segment.split()
            if _command_is_inline_python(words, "<<" in segment or "<<<" in segment):
                found.append(line_number)
    return found


def _scan_script(shell: str, script: str) -> list[tuple[int, str]]:
    if _is_python_shell(shell):
        return [(1, "inline-python")]
    if _is_powershell_shell(shell):
        found = list(powershell_issue_lines(script))
        found.extend((line, "inline-python") for line in _powershell_inline_lines(script))
        return _unique_lines(found)
    return _bash_issue_lines(script)


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
    return bool(stripped) and (stripped[0] in "&*!%" or stripped.startswith("<<"))


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


def _shell_from_defaults(node: object, inherited: str) -> str:
    if not isinstance(node, _YamlMap):
        return inherited
    defaults = node.get("defaults")
    if not isinstance(defaults, _YamlMap):
        return inherited
    run = defaults.get("run")
    if not isinstance(run, _YamlMap):
        return inherited
    shell = run.get("shell")
    if isinstance(shell, _YamlScalar):
        return shell.text.strip() or inherited
    return inherited


def _visit_yaml(node: object, inherited: str, findings: list[tuple[int, str]]) -> None:
    if isinstance(node, _YamlSeq):
        for item in node.items:
            _visit_yaml(item, inherited, findings)
        return
    if not isinstance(node, _YamlMap):
        return
    outgoing = _shell_from_defaults(node, inherited)
    shell_value = node.get("shell")
    run_value = node.get("run")
    step_shell = outgoing
    if isinstance(shell_value, _YamlScalar):
        step_shell = shell_value.text.strip() or outgoing
        if _is_python_shell(shell_value.text):
            findings.append((shell_value.line, "inline-python"))
    if isinstance(run_value, _YamlScalar):
        if _is_python_shell(step_shell):
            findings.append((run_value.line, "inline-python"))
        else:
            for script_line, kind in _scan_script(step_shell, run_value.text):
                if run_value.line_map and 1 <= script_line <= len(run_value.line_map):
                    findings.append((run_value.line_map[script_line - 1], kind))
                else:
                    findings.append((run_value.line, kind))
    for _key, _line, child in node.entries:
        _visit_yaml(child, outgoing, findings)


def yaml_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    if normalized.startswith("\ufeff"):
        return [(1, "unscanned")]
    document = _YamlDocument(normalized.split("\n"))
    document.parse()
    if document.root is not None and not document.failed:
        _visit_yaml(document.root, "bash", document.findings)
    elif document.root is not None:
        _visit_yaml(document.root, "bash", document.findings)
    return _unique_lines(document.findings)


def ini_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    return [(_line_number(starts, start), "comment") for start, _end in _hash_comment_spans(normalized, markers="#;")]

def slash_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = [(_line_number(starts, start), "comment") for start, _end in _slash_spans(normalized)]
    return found


def _slash_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char in "'\"" or (char == "`"):
            quote = char
            index += 1
            while index < size:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if text.startswith("//", index):
            start = index
            while index < size and text[index] != "\n":
                index += 1
            spans.append((start, index))
            continue
        if text.startswith("/*", index):
            start = index
            index += 2
            while index < size and not text.startswith("*/", index):
                index += 1
            index = min(size, index + 2)
            spans.append((start, index))
            continue
        index += 1
    return spans


def css_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = []
    index = 0
    while True:
        start = normalized.find("/*", index)
        if start < 0:
            break
        end = normalized.find("*/", start + 2)
        end = len(normalized) if end < 0 else end + 2
        found.append((_line_number(starts, start), "comment"))
        index = end
    return found


def html_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = []
    index = 0
    marker = "<!--"
    while True:
        start = normalized.find(marker, index)
        if start < 0:
            break
        found.append((_line_number(starts, start), "comment"))
        end = normalized.find("-->", start + len(marker))
        index = len(normalized) if end < 0 else end + 3
    return found


def _normalized_path(path: str) -> str:
    return path.replace("\\", "/").casefold()


def _kind(path: str) -> str | None:
    normalized = _normalized_path(path)
    name = normalized.rsplit("/", 1)[-1]
    suffix = Path(normalized).suffix
    if suffix in _PY_SUFFIXES:
        return "python"
    if suffix == ".ps1":
        return "powershell"
    if suffix in {".bat", ".cmd"}:
        return "batch"
    if suffix == ".iss":
        return "inno"
    if suffix == ".sh":
        return "shell"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix in {".ini", ".cfg"}:
        return "ini"
    if suffix in _HASH_SUFFIXES:
        return "hashconfig"
    if name in {".gitignore", ".gitattributes"}:
        return "gitignore"
    if suffix in _SLASH_SUFFIXES:
        return "slash"
    if suffix == ".css":
        return "css"
    if suffix in {".html", ".htm", ".svg", ".xml"}:
        return "html"
    if name.startswith("requirements") and suffix == ".txt":
        return "hashconfig"
    if suffix in _DATA_SUFFIXES:
        return None
    return "unscanned"


def _issue_lines(kind: str, text: str) -> list[tuple[int, str]]:
    if kind == "python":
        return python_issue_lines(text)
    if kind == "powershell":
        return powershell_issue_lines(text)
    if kind == "batch":
        return batch_issue_lines(text)
    if kind == "inno":
        return inno_issue_lines(text)
    if kind == "shell":
        return shell_issue_lines(text)
    if kind == "yaml":
        return yaml_issue_lines(text)
    if kind == "hashconfig":
        return hash_config_issue_lines(text)
    if kind == "ini":
        return ini_issue_lines(text)
    if kind == "gitignore":
        return gitignore_issue_lines(text)
    if kind == "slash":
        return slash_issue_lines(text)
    if kind == "css":
        return css_issue_lines(text)
    if kind == "html":
        return html_issue_lines(text)
    return []


def strip_text(kind: str, text: str) -> str:
    if kind == "python":
        return strip_python(text)
    style = _newline_style(text)
    normalized = _normalize_newlines(text)
    if kind == "powershell":
        stripped = _delete_spans(normalized, _powershell_spans(normalized))
    elif kind == "shell":
        stripped = _delete_spans(normalized, _shell_comment_spans(normalized))
    elif kind == "yaml":
        stripped = _strip_yaml(normalized)
    elif kind == "hashconfig":
        stripped = _delete_spans(normalized, _hash_comment_spans(normalized))
    elif kind == "ini":
        stripped = _delete_spans(normalized, _hash_comment_spans(normalized, markers="#;"))
    elif kind == "gitignore":
        kept = [line for line in normalized.split("\n") if not line.lstrip(" \t").startswith("#")]
        stripped = "\n".join(kept)
        if normalized.endswith("\n"):
            stripped += "\n" if stripped and not stripped.endswith("\n") else ""
            if normalized.endswith("\n") and not stripped.endswith("\n"):
                stripped += "\n"
    elif kind == "batch":
        kept = []
        for line in normalized.split("\n"):
            stripped_line = line.lstrip(" \t")
            folded = stripped_line.upper()
            if folded == "REM" or folded.startswith("REM ") or folded.startswith("REM\t") or stripped_line.startswith("::"):
                continue
            kept.append(line)
        stripped = "\n".join(kept)
        if normalized.endswith("\n") and not stripped.endswith("\n"):
            stripped += "\n"
    elif kind == "inno":
        stripped = _strip_inno(normalized)
    elif kind == "slash":
        stripped = _delete_spans(normalized, _slash_spans(normalized))
    elif kind == "css":
        stripped = normalized
        while "/*" in stripped:
            start = stripped.find("/*")
            end = stripped.find("*/", start + 2)
            end = len(stripped) if end < 0 else end + 2
            stripped = stripped[:start] + stripped[end:]
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
    findings: list[Finding] = []
    for line, issue in _issue_lines(kind, text):
        if issue == "comment" and _directive_allowed(path, _line_text(text, line)):
            continue
        findings.append(Finding(path, line, issue))
    return findings


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
        text = (base / relative).read_text(encoding="utf-8")
        findings.extend(issues_for(relative, text))
    return findings


def _line_text(text: str, line: int) -> str:
    return _normalize_newlines(text).split("\n")[line - 1]
