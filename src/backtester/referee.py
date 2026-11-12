"""One-bar referee.

Not implemented. The later protocol sends bar t, waits for the decision, then
sends bar t+1. A timeout is look-ahead. Warm-up follows the active profile:
Arena rescales and keeps exposure; Desk stays flat until live.

An anonymized or pack replay is a designer surface. It must price fills with
``costs_designer`` (the flat per-profile fee) and the canonical designer
equity, and it must not read ``share_scale``. The operator copy of a final
result keeps the real fee schedule and the real share counts. This module
does not implement that replay. ``designer_run.execute`` is the hook.

This module has no operator paths and does not read a secrets file.
"""


def replay(*_args, **_kwargs):
    raise NotImplementedError("The referee is a later phase.")
