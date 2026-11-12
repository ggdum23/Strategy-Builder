"""``python -m backtester.gui`` opens the same window as ``python -m backtester gui``."""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
