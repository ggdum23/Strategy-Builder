"""Decide whether a silent uninstall has really finished.

Inno's first-phase unins000.exe exits before CurUninstallStepChanged(usPostUninstall).
A check that only looks at the profile directory in that gap passes even when
the second phase is about to delete it. These functions return "ok" only after
the uninstall log records the decision, and they fail if the profile disappears
or the opposite decision is logged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KEEPING = "Strategy Builder keeping "
DELETING = "Strategy Builder deleting "


def judge_keep(log_text: str, config_text: str | None, *, sentinel: str) -> str:
    """Return "wait" or "ok". Raise SystemExit when the keep contract breaks.

    "ok" requires the keeping line and the original config, still carrying
    the upgrade sentinel. An empty log while the config remains is "wait",
    which is the gap after the first phase exits.
    """
    log_text = log_text or ""
    if DELETING in log_text:
        raise SystemExit("uninstall log says it is deleting the profile")
    if config_text is None:
        raise SystemExit("profile config disappeared before the keeping line")
    if not config_text.strip():
        return "wait"
    try:
        payload = json.loads(config_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"profile config is not json: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("profile config is not an object")
    if payload.get("data_root") != sentinel:
        raise SystemExit(
            f"profile data_root is {payload.get('data_root')!r}, expected the upgrade sentinel"
        )
    if payload.get("ci_marker") != "upgrade-keep":
        raise SystemExit("profile config lost the upgrade ci_marker")
    if KEEPING not in log_text:
        return "wait"
    return "ok"


def judge_delete(log_text: str, *, root_exists: bool) -> str:
    """Return "wait" or "ok". A missing directory without the deleting line is not success."""
    log_text = log_text or ""
    if KEEPING in log_text:
        raise SystemExit("DELETEUSERDATA was ignored; uninstall kept the profile")
    if DELETING in log_text and not root_exists:
        return "ok"
    return "wait"


def _read(path: Path) -> tuple[str, bool]:
    """Return (text, exists). A locked file exists with empty text so the caller waits."""
    if not path.exists():
        return "", False
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace"), True
    except OSError:
        return "", True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Judge a Strategy Builder uninstall log")
    parser.add_argument("--mode", choices=("keep", "delete"), required=True)
    parser.add_argument("--sentinel", required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root-exists", choices=("yes", "no"), required=True)
    args = parser.parse_args(argv)
    log_text, _exists = _read(args.log_file)
    try:
        if args.mode == "keep":
            config_text, present = _read(args.config)
            verdict = judge_keep(log_text, config_text if present else None, sentinel=args.sentinel)
        else:
            verdict = judge_delete(log_text, root_exists=args.root_exists == "yes")
    except SystemExit as exc:
        message = exc.code if isinstance(exc.code, str) and exc.code else "uninstall guard failed"
        print(f"fail {message}")
        return 1
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
