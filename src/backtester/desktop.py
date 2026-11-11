"""Window entry. ``python -m backtester gui`` and a frozen executable both call this.

The implementation lives in ``backtester.gui``. Importing this module does not
import Qt; ``main`` does, so a CLI install without the gui extra still imports.
"""

from __future__ import annotations


def main() -> int:
    from .gui.app import main as gui_main

    return int(gui_main())
