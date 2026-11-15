
from __future__ import annotations


def main() -> int:
    from .gui.app import main as gui_main

    return int(gui_main())
