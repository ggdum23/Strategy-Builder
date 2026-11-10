"""One-bar referee.

Not implemented. The later protocol sends bar t, waits for the decision, then
sends bar t+1. A timeout is look-ahead. Warm-up follows the active profile:
Arena rescales and keeps exposure; Desk stays flat until live.

This module has no operator paths and does not read a secrets file.
"""


def replay(*_args, **_kwargs):
    raise NotImplementedError("The referee is a later phase.")
