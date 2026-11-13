"""Allocate until the memory cap raises MemoryError.

Returns 0.0 when the allocation is refused and 1.0 if a large block was
allocated anyway.
"""


class Strategy:
    def on_bar(self, bar, state):
        blocks = []
        try:
            for _ in range(32):
                blocks.append(bytearray(32 * 1024 * 1024))
        except MemoryError:
            return 0.0
        return 1.0
