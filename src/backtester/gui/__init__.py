"""Desktop research terminal.

Importing this package does not import Qt and does not read market data.
Spawned run workers import this package; keeping Qt out of it avoids starting
another window inside those processes. The window entry is ``backtester.gui.app``.
"""

from __future__ import annotations

__all__: list[str] = []
