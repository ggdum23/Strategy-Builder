from __future__ import annotations

import ast
import subprocess
import tokenize
from io import StringIO
from pathlib import Path

FILE_ALLOWLIST = (
    "src/backtester/costs.py",
    "src/backtester/costs_desk.py",
)

DIRECTIVE_ALLOWLIST: tuple[tuple[str, str], ...] = ()

_DOC_PARENTS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_HASH_SUFFIXES = {".toml", ".ini", ".cfg"}
_SLASH_SUFFIXES = {".js", ".mjs", ".cjs", ".qml"}
_PY_SUFFIXES = {".py", ".spec"}


class Finding:
    def __init__(self, path: str, line: int, kind: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind

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


def _is_string_statement(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


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
    for node in ast.walk(tree):
        if not isinstance(node, _DOC_PARENTS) or not node.body or not _is_string_statement(node.body[0]):
            continue
        statement = node.body[0]
        start = _position(starts, statement.lineno, statement.col_offset)
        end = _position(starts, statement.end_lineno, statement.end_col_offset)
        needs_pass = not isinstance(node, ast.Module) and len(node.body) == 1
        if needs_pass:
            spans.append((start, end, "pass"))
            continue
        if _outside_is_whitespace(normalized, starts, start, end):
            start, end = _expand_lines(normalized, starts, start, end)
        spans.append((start, end, ""))
    spans.sort()
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


def _quote_spans_and_hashes(text: str, quote: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    size = len(text)
    while index < size:
        if text.startswith(quote * 3, index):
            index += 3
            while index < size and not text.startswith(quote * 3, index):
                index += 1
            index = min(size, index + 3)
            continue
        char = text[index]
        if char == quote:
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
        if char == "#":
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
    found = [(_line_number(starts, start), "comment") for start, _end in _quote_spans_and_hashes(normalized, '"')]
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
    normalized = _normalize_newlines(text)
    starts = _line_starts(normalized)
    found = [(_line_number(starts, start), "comment") for start, _end in _shell_comment_spans(normalized)]
    return found


def _shell_comment_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    size = len(text)
    while index < size:
        if text.startswith("<<", index):
            index, body_start, body_end = _skip_heredoc(text, index)
            body = text[body_start:body_end]
            if "#" in body:
                try:
                    ast.parse(body)
                except SyntaxError:
                    pass
                else:
                    for start, end, _replacement in python_spans(body):
                        spans.append((body_start + start, body_start + end))
            continue
        char = text[index]
        if char == "'":
            index += 1
            while index < size and text[index] != "'":
                index += 1
            index = min(size, index + 1)
            continue
        if char == '"':
            index += 1
            while index < size:
                if text[index] == "\\":
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


def _skip_heredoc(text: str, index: int) -> tuple[int, int, int]:
    cursor = index + 2
    if cursor < len(text) and text[cursor] == "-":
        cursor += 1
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    if cursor < len(text) and text[cursor] in "'\"":
        quote = text[cursor]
        cursor += 1
        end = text.find(quote, cursor)
        word = text[cursor:end if end >= 0 else len(text)]
        cursor = end + 1 if end >= 0 else len(text)
    else:
        end = cursor
        while end < len(text) and text[end] not in " \t\n;|&<>":
            end += 1
        word = text[cursor:end]
        cursor = end
    newline = text.find("\n", cursor)
    if newline < 0:
        return len(text), len(text), len(text)
    body_start = newline + 1
    cursor = body_start
    while cursor < len(text):
        newline = text.find("\n", cursor)
        if newline < 0:
            newline = len(text)
        if text[cursor:newline].strip() == word:
            return newline, body_start, cursor
        cursor = newline + 1 if newline < len(text) else len(text)
    return len(text), body_start, len(text)


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


def _dedent(lines: list[str]) -> str:
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    if not indents:
        return "\n".join(lines)
    cut = min(indents)
    dedented = []
    for line in lines:
        if line.strip():
            dedented.append(line[cut:])
        else:
            dedented.append("")
    return "\n".join(dedented)


def _block_key(stripped: str) -> str | None:
    code = stripped
    mark = _yaml_hash_index(stripped)
    if mark is not None:
        code = stripped[:mark]
    code = code.rstrip()
    indicators = ("|+", "|-", ">", ">+", ">-", "|", ">")
    for indicator in indicators:
        if code.endswith(indicator) and (len(code) == len(indicator) or code[-len(indicator) - 1 : -len(indicator)].strip() in {"", ":"} or code.endswith(":" + indicator) or code.endswith(": " + indicator)):
            head = code[: -len(indicator)].rstrip()
            if ":" in head:
                return head.split(":")[0].strip()
            return head.strip()
    return None


def _yaml_hash_index(line: str) -> int | None:
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
            double = not double
        elif char == "#" and not single and not double:
            return index
        index += 1
    return None


def yaml_issue_lines(text: str) -> list[tuple[int, str]]:
    lines = _normalize_newlines(text).split("\n")
    found: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip(" ")
        if stripped == "" or stripped.startswith("#"):
            if stripped.startswith("#"):
                found.append((index + 1, "comment"))
            index += 1
            continue
        header_indent = len(line) - len(stripped)
        key = _block_key(stripped)
        inline = _yaml_hash_index(line)
        if key is None:
            if inline is not None:
                found.append((index + 1, "comment"))
            index += 1
            continue
        if inline is not None and inline < len(line.rstrip()):
            before = line[:inline].rstrip()
            if not before.endswith(("|", ">", "|-", "|+", ">-", ">+")):
                found.append((index + 1, "comment"))
        index += 1
        body_start = index
        while index < len(lines):
            nxt = lines[index]
            if nxt.strip() == "":
                index += 1
                continue
            indent = len(nxt) - len(nxt.lstrip(" "))
            if indent <= header_indent:
                break
            index += 1
        if key != "run":
            continue
        body = lines[body_start:index]
        shell = _step_shell(lines, body_start)
        script = _dedent(body)
        if shell in {"pwsh", "powershell"}:
            issues = powershell_issue_lines(script)
        else:
            issues = shell_issue_lines(script)
        for script_line, kind in issues:
            found.append((body_start + script_line, kind))
    return _unique_lines(found)


def _step_shell(lines: list[str], body_start: int) -> str:
    cursor = body_start - 2
    while cursor >= 0:
        stripped = lines[cursor].strip()
        if stripped.startswith("- "):
            break
        if stripped.startswith("shell:"):
            return stripped.split(":", 1)[1].strip().strip("\"'")
        cursor -= 1
    return "bash"


def _kind(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    suffix = Path(path).suffix.lower()
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
    if suffix in _HASH_SUFFIXES:
        return "hashconfig"
    if name in {".gitignore", ".gitattributes"}:
        return "gitignore"
    if suffix in _SLASH_SUFFIXES:
        return "slash"
    if suffix == ".css":
        return "css"
    if suffix in {".html", ".htm"}:
        return "html"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "hashconfig"
    return None


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
        stripped = _delete_spans(normalized, _quote_spans_and_hashes(normalized, '"'))
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
    remove: set[int] = set()
    edits: dict[int, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip(" ")
        if stripped.startswith("#"):
            remove.add(index)
            index += 1
            continue
        header_indent = len(line) - len(stripped) if stripped else 0
        key = _block_key(stripped) if stripped else None
        inline = _yaml_hash_index(line) if stripped and key is None else None
        if inline is not None:
            edits[index] = line[:inline].rstrip()
        if key is None:
            index += 1
            continue
        index += 1
        body_start = index
        while index < len(lines):
            nxt = lines[index]
            if nxt.strip() == "":
                index += 1
                continue
            indent = len(nxt) - len(nxt.lstrip(" "))
            if indent <= header_indent:
                break
            index += 1
        if key != "run":
            continue
        body = lines[body_start:index]
        shell = _step_shell(lines, body_start)
        script_lines = _dedent(body).split("\n")
        script = "\n".join(script_lines)
        if shell in {"pwsh", "powershell"}:
            issues = {line_number for line_number, _kind in powershell_issue_lines(script)}
        else:
            issues = {line_number for line_number, _kind in shell_issue_lines(script)}
        for script_line in issues:
            remove.add(body_start + script_line - 1)
    kept = []
    for number, line in enumerate(lines):
        if number in remove:
            continue
        kept.append(edits.get(number, line))
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


def _is_documentation(path: str) -> bool:
    if path.startswith("docs/"):
        return True
    if path.endswith(".md"):
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


def scan(root: Path | None = None) -> list[Finding]:
    base = root or repo_root()
    findings: list[Finding] = []
    for relative in tracked_paths(base):
        if relative in FILE_ALLOWLIST or _is_documentation(relative):
            continue
        kind = _kind(relative)
        if kind is None:
            continue
        text = (base / relative).read_text(encoding="utf-8")
        for line, issue in _issue_lines(kind, text):
            if issue == "comment" and _directive_allowed(relative, _line_text(text, line)):
                continue
            findings.append(Finding(relative, line, issue))
    return findings


def _line_text(text: str, line: int) -> str:
    return _normalize_newlines(text).split("\n")[line - 1]
