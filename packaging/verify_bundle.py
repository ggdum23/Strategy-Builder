"""Fail the build if the one-folder bundle is missing a required piece."""

from __future__ import annotations

import sys
from pathlib import Path


def _exe(directory: Path, stem: str) -> Path:
    windows = directory / f"{stem}.exe"
    if windows.is_file():
        return windows
    plain = directory / stem
    if plain.is_file():
        return plain
    raise SystemExit(f"missing executable {stem!r} in {directory}")


def _has_child_files(directory: Path, name: str) -> bool:
    for candidate in directory.rglob(name):
        if candidate.is_dir() and any(path.is_file() for path in candidate.iterdir()):
            return True
    return False


def verify(directory: Path) -> None:
    directory = directory.resolve()
    if not directory.is_dir():
        raise SystemExit(f"bundle directory does not exist: {directory}")
    internal = directory / "_internal"
    if not internal.is_dir():
        raise SystemExit(
            f"{directory} has no _internal directory. The build must be one-folder, not one-file."
        )
    cli = _exe(directory, "backtester")
    gui = _exe(directory, "Strategy Builder")
    if cli.stat().st_size <= 0 or gui.stat().st_size <= 0:
        raise SystemExit("an executable is empty")
    # A one-file executable is tens of megabytes and has no sibling _internal.
    # The onedir bootloader is the small stub; libraries live beside it.
    if cli.stat().st_size > 30_000_000 and not any(internal.iterdir()):
        raise SystemExit("backtester looks like a one-file build")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import collect_optional

    if collect_optional.installed("matplotlib"):
        mpl = list(internal.rglob("matplotlibrc"))
        mpl = [path for path in mpl if "mpl-data" in path.parts]
        if not mpl:
            raise SystemExit("matplotlib is installed but mpl-data was not collected")
    if collect_optional.installed("PySide6"):
        for kind in collect_optional.QT_PLUGIN_KINDS:
            if not _has_child_files(internal, kind):
                raise SystemExit(f"PySide6 is installed but the {kind} plugin directory is missing")
    if collect_optional.installed("pyqtgraph"):
        found = list(internal.rglob("pyqtgraph"))
        if not any(path.is_dir() for path in found):
            raise SystemExit("pyqtgraph is installed but was not collected")
    print(f"bundle ok: {cli.name}, {gui.name}, _internal")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("usage: verify_bundle.py DIST_DIR")
    verify(Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
