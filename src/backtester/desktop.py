"""Seam for a later desktop window.

Phase 1 does not draw a GUI. A future front end imports runner, data, and
report. Those modules do not import this one, and they do not need a console.
"""


def main() -> None:
    raise NotImplementedError(
        "The desktop GUI is not in this phase. Run `python -m backtester run`."
    )
