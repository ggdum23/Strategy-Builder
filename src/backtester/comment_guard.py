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

_HASH_SUFFIXES = {".toml"}
_PY_SUFFIXES = {".py", ".pyw", ".spec"}
_DOC_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
_DATA_SUFFIXES = {".csv", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".png", ".webp", ".xz", ".zip"}
_PROSE_TEXT_NAMES = {"license.txt", "install_before.txt"}
_DATA_TEXT_NAMES = {"engine_sha256.txt"}
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


def python_issue_lines(text: str) -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    try:
        spans = python_spans(text)
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


def inno_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    found = []
    for line_number, line in enumerate(normalized.split("\n"), 1):
        stripped = line.lstrip(" \t")
        if stripped.startswith(";"):
            found.append((line_number, "comment"))
            continue
        mark = _inno_slash_comment(line)
        if mark == -1:
            found.append((line_number, "unscanned"))
        elif mark is not None:
            found.append((line_number, "comment"))
    return found


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
_BANNED_COMMANDS = {"eval", "bash", "sh", "dash", "pwsh", "powershell", "bash.exe", "sh.exe", "pwsh.exe", "powershell.exe"}
_INTERPRETER_PREFIXES = ("python", "pypy", "node", "perl", "ruby", "php")


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


def _is_inline_interpreter(token: str) -> bool:
    base = token.casefold().rsplit("/", 1)[-1]
    if base.endswith(".exe"):
        base = base[:-4]
    for prefix in _INTERPRETER_PREFIXES:
        if base == prefix or base.startswith(prefix):
            return True
    return False


def _command_allowed(tokens: list[str]) -> bool:
    if not tokens:
        return False
    for token in tokens:
        folded = token.casefold()
        base = folded.rsplit("/", 1)[-1]
        if folded.startswith("-c") or folded in _BANNED_COMMANDS or base in _BANNED_COMMANDS:
            return False
    if _is_inline_interpreter(tokens[0]) and "-" in tokens:
        return False
    return True


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


def _visit_yaml(node: object, findings: list[tuple[int, str]]) -> None:
    if isinstance(node, _YamlSeq):
        for item in node.items:
            _visit_yaml(item, findings)
        return
    if not isinstance(node, _YamlMap):
        return
    seen: set[str] = set()
    for key, line, child in node.entries:
        if key in seen:
            findings.append((line, "unscanned"))
        seen.add(key)
        if key == "shell":
            if not isinstance(child, _YamlScalar) or child.text.strip().casefold() not in _ALLOWED_SHELLS:
                findings.append((child.line if isinstance(child, _YamlScalar) else line, "inline-python"))
        elif key == "run":
            if not isinstance(child, _YamlScalar):
                findings.append((line, "unscanned"))
            else:
                _check_run(child, findings)
        _visit_yaml(child, findings)


def yaml_issue_lines(text: str) -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]
    normalized = _normalize_newlines(text)
    document = _YamlDocument(normalized.split("\n"))
    document.parse()
    if document.root is not None:
        _visit_yaml(document.root, document.findings)
    return _unique_lines(document.findings)


def ini_issue_lines(text: str) -> list[tuple[int, str]]:
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    return [(_line_number(starts, start), "comment") for start, _end in _hash_comment_spans(normalized, markers="#;")]

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


def json_issue_lines(text: str) -> list[tuple[int, str]]:
    if _rejected_text(text):
        return [(1, "unscanned")]

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
        json.loads(text, object_pairs_hook=pairs)
    except (json.JSONDecodeError, ValueError, UnicodeError):
        return [(1, "unscanned")]
    return []


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
    if suffix == ".txt" and name in _PROSE_TEXT_NAMES | _DATA_TEXT_NAMES:
        return None
    if suffix in _DATA_SUFFIXES:
        return None
    return "unscanned"


def _issue_lines(kind: str, text: str) -> list[tuple[int, str]]:
    if kind == "python":
        return python_issue_lines(text)
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
        return json_issue_lines(text)
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
        parsed = _issue_lines(kind, text)
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
