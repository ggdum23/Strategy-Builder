"""Seam for a later desktop window.

This phase does not draw a GUI. A future front end calls
``backtester.parallel.run_parallel`` and ``backtester.report.load_report``.
Those functions take a progress callback, a cancel token, and return
structured results. They do not print, and they do not import this module.
"""


def main() -> None:
    raise NotImplementedError(
        "The desktop GUI is not in this phase. Run `python -m backtester run`."
    )
