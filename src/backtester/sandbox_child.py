"""Child shim for the one-bar pipe. Not implemented.

The parent will launch `python -I` with a cleared environment and a private
directory that contains only the strategy and this shim. One bar per
round-trip. No network and no strategy filesystem access.

Operator secret paths are not embedded here.
"""


def main() -> None:
    raise NotImplementedError("The referee child is a later phase.")
