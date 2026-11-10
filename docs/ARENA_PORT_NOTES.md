# Arena port notes

`src/backtester/engine_arena.py` is the reference `backtest.py`.
`src/backtester/costs.py` is the reference `costs.py`, byte for byte.
`src/backtester/arena_runner.py` is the reference `run_backtest.py` with the
import pointed at this package. The verbatim files, including the original
`tests/test_engine.py`, live under `tests/reference/arena/` and are checked
against `ENGINE_SHA256.txt`.

The textual differences in `engine_arena.py` are:

- The module docstring, plus a comment that `WINDOWS` (design 0..2999,
  validation 3000..3795, 504-session warm-up) are the anonymized QQQ pack cuts
  and are not a global default.
- `from . import costs as C` instead of `import costs`.
- `_read_chunk` no longer writes a pickle beside the source. An installed
  package must not write next to itself. The frame is the one `pandas.read_csv`
  returned.
- `run(..., borrow_rate_annual=None)`. `None` calls `costs.daily_borrow`, the
  original line. Any other value is an explicit override. The default path is
  what the parity test compares.

PLAN.md says the Arena FINRA TAF is $0. The reference `costs.py` charges
`$0.000195` per share sold, maximum `$9.79`, and SEC `$0.0000206` of sale value.
Parity with the vendored engine wins. Those IBKR constants are not edited to
match the plan sentence. Bit-identity follows the reference file. The constants
are pinned by a test that compares them to the literals, not only to themselves.
The same decision is stated in `docs/DESK_SPEC_DECISIONS.md`.

PLAN.md lists Arena state keys `position_shares`, `position_value`, and
`avg_price`. The reference state dict, which `D02` and `D08` read via
`state["target"]`, is `equity`, `cash`, `shares`, `position`, `target`,
`pending_order`. The port keeps the reference dict.

The original manifest test hashes `bars_1min_part01..08.csv.xz` and
`daily_rth.csv`. Those bytes were not in the archive. They are not committed.
The ported test pins the source hashes that do exist and asserts the missing
parts are absent. The original test function is unchanged inside
`ref_test_engine.py`.

`arena_runner.main` still understands `full`, `design`, and `validation`.
Nothing in `python -m backtester run` or `walkforward` calls it, and walk-forward
has no default cut. Importing the runner does not insert its directory on
`sys.path` (the reference script did, at import). The named CLI writes strict
JSON, with non-finite floats as null. The pack runner keeps the reference
`json.dump`, which can emit non-standard tokens.

Arena financing stays 0 in this phase. A non-zero `--financing-rate`, a
`--fee-mult` other than 1, or a dividend file on an Arena run is an error.
Those switches belong to Desk. Adding Arena financing is later work and would
change the reference loop.
