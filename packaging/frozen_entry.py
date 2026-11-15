
from __future__ import annotations

import sys
from pathlib import Path


def _prefer_agg_backend() -> None:
    import os

    if "--sandbox-child" in sys.argv:
        sys.argv = ["strategy"]
        from backtester.sandbox_child import main as sandbox_main

        raise SystemExit(sandbox_main())

    os.environ["MPLBACKEND"] = "Agg"
    argv = sys.argv[1:]
    if "-c" in argv and any("resource_tracker" in part or "forkserver" in part for part in argv):
        return
    import matplotlib

    matplotlib.use("Agg")


def _emit(text: str) -> None:
    stream = sys.stdout
    if stream is None:
        return
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _ensure_stdio() -> None:
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _call(function, argv) -> int:
    try:
        code = function(argv)
    except SystemExit as exc:
        code = exc.code
    if code is None:
        return 0
    return int(code)


def version_text() -> str:
    from backtester import __version__

    return __version__


def is_gui_executable(executable: str | Path | None = None) -> bool:
    exe = Path(sys.executable if executable is None else executable)
    stem = exe.stem.casefold().replace(" ", "")
    return stem == "strategybuilder"


def _gui_package_present() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("backtester.gui") is not None
    except (ImportError, ValueError):
        return False


def _gui_subcommand_available() -> bool:
    import argparse

    try:
        from backtester.__main__ import _build_parser
    except Exception:
        return False
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return "gui" in action.choices
    return False


def _gui_module_main():
    try:
        import backtester.gui as gui
    except Exception:
        return None
    function = getattr(gui, "main", None)
    if callable(function):
        return function
    return None


def _ensure_config(environ):
    import strategy_builder_config

    return strategy_builder_config.ensure_user_config(environ)


def placeholder_text(config: dict) -> str:
    data_root = str(config.get("data_root") or "").strip() or "(not set)"
    runs = str(config.get("runs_dir") or "").strip() or "(not set)"
    return (
        "Strategy Builder\n"
        "\n"
        "The desktop window is not part of this build.\n"
        "\n"
        "The command-line backtester is installed beside this program as backtester.exe.\n"
        "Example:\n"
        "  backtester.exe run --profile arena --ticker QQQ --strategy strategy.py --src <filled-folder> --out <run-folder>\n"
        "\n"
        f"Market data folder from your settings:\n  {data_root}\n"
        "\n"
        f"Backtests and the registry are kept in:\n  {runs}\n"
        "\n"
        "Uninstall does not delete that folder unless you ask it to.\n"
        "This program does not copy market data."
    )


def show_placeholder(config: dict, *, interactive: bool | None = None) -> int:
    import os

    text = placeholder_text(config)
    if interactive is None:
        interactive = sys.platform == "win32" and os.environ.get("STRATEGY_BUILDER_HEADLESS") != "1"
    if not interactive:
        _emit(text)
        return 0
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "Strategy Builder", 0x00000040)
    return 0


def _launcher_check(argv: list[str]) -> int:
    report = None
    if "--launcher-report" in argv:
        index = argv.index("--launcher-report")
        if index + 1 >= len(argv):
            _emit("error: --launcher-report needs a path")
            return 2
        report = Path(argv[index + 1])
    mode = "gui" if _gui_package_present() else "placeholder"
    text = f"launcher-check: {mode}\n"
    _emit(text)
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
    return 0


def launch_cli(argv: list[str]) -> int:
    if argv and argv[0] in {"--version", "-V", "version"}:
        _emit(version_text())
        return 0
    from backtester.__main__ import main as cli_main

    return _call(cli_main, argv)


def _call_gui_module(function, extra: list[str]) -> int:
    if not extra:
        try:
            return _call(function, [])
        except TypeError:
            result = function()
            return int(result or 0)
    try:
        return _call(function, extra)
    except TypeError:
        result = function()
        return int(result or 0)


def launch_gui(argv: list[str]) -> int:
    _ensure_stdio()
    if "--launcher-check" in argv:
        return _launcher_check(argv)
    if not _gui_package_present():
        return show_placeholder(_ensure_config(None))
    extra = [arg for arg in argv if arg != "--launcher-check"]
    if _gui_subcommand_available():
        from backtester.__main__ import main as cli_main

        return _call(cli_main, ["gui", *extra])
    function = _gui_module_main()
    if function is not None:
        return _call_gui_module(function, extra)
    _emit("backtester.gui is installed but it has no main() and the CLI has no gui command.")
    return 2


def main(argv: list[str] | None = None, *, gui: bool | None = None, environ: dict | None = None) -> int:
    config_env = environ
    _ensure_config(config_env)
    args = list(sys.argv[1:] if argv is None else argv)
    use_gui = is_gui_executable() if gui is None else gui
    if use_gui:
        if "--launcher-check" in args:
            _ensure_stdio()
            return _launcher_check(args)
        if not _gui_package_present():
            import strategy_builder_config

            return show_placeholder(strategy_builder_config.ensure_user_config(config_env))
        return launch_gui(args)
    return launch_cli(args)


if __name__ == "__main__":
    import multiprocessing
    import multiprocessing.spawn

    _prefer_agg_backend()
    multiprocessing.spawn.freeze_support()
    multiprocessing.freeze_support()
    raise SystemExit(main())
