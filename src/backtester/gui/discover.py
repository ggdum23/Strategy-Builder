"""Find strategy files without executing them."""

from __future__ import annotations

import ast
from pathlib import Path


def discover_strategies(path) -> list[Path]:
    """One Python file, or every ``strategy.py`` under a folder.

    A folder search does not pick up arbitrary modules. A file the operator
    points at is accepted under any name; the AST check then looks for ``Strategy``.
    """
    file = Path(path)
    if file.is_file():
        if file.suffix.lower() != ".py":
            raise ValueError(f"{file} is not a Python file")
        return [file]
    if not file.is_dir():
        raise FileNotFoundError(f"strategy path does not exist: {file}")
    found = sorted(item for item in file.rglob("strategy.py") if item.is_file())
    if not found:
        raise FileNotFoundError(f"no strategy.py under {file}")
    return found


def summarize_strategy(path) -> dict:
    """Static summary. The file is parsed, not imported."""
    file = Path(path)
    record = {
        "path": str(file),
        "name": file.parent.name if file.name == "strategy.py" else file.stem,
        "has_strategy": False,
        "wants_extended_hours": None,
        "error": None,
        "profile_excerpt": None,
    }
    try:
        source = file.read_text(encoding="utf-8")
    except OSError as exc:
        record["error"] = str(exc)
        return record
    try:
        tree = ast.parse(source, filename=str(file))
    except SyntaxError as exc:
        record["error"] = f"syntax error: {exc.msg} (line {exc.lineno})"
        return record
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Strategy":
            record["has_strategy"] = True
            record["wants_extended_hours"] = _extended_hours(node)
            break
    note = file.parent / "PROFILE.md" if file.name == "strategy.py" else None
    if note is not None and note.is_file():
        lines = note.read_text(encoding="utf-8", errors="replace").splitlines()
        excerpt = "\n".join(lines[:12]).strip()
        if excerpt:
            record["profile_excerpt"] = excerpt
    return record


def _extended_hours(node: ast.ClassDef):
    for stmt in node.body:
        targets = []
        value = None
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "WANTS_EXTENDED_HOURS":
                if isinstance(value, ast.Constant) and isinstance(value.value, bool):
                    return value.value
                return None
    return None
