# Vendored Arena reference

`arena/` is the reference pack, byte for byte, hashed in `ENGINE_SHA256.txt`.
Do not edit those files. The bar parts and `daily_rth.csv` were not in the
archive; only their hashes were. They are not committed.

`src/backtester/costs.py` is that `costs.py`. `engine_arena.py` is the port.
`ref_test_engine.py` is the original `tests/test_engine.py` under a name pytest
does not collect.
