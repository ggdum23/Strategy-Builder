# Performance

A named series is parsed once and reused. Later runs and parallel workers map that result. The engines, the fee schedules, and the one-bar jail protocol are unchanged: a strategy still sees bar `t` only after it has decided bar `t-1`, and it never receives the series as a file.

`costs.py` is `a3b0a522d8bf469001e75aaf274b0c5d3cbef624a68ed9fb94d29298110fe281`. `costs_desk.py` is `07b0d25ceeebe292f03a2b7b6ca0d76d76fa8d6a59f21d60103abfceeab0ebbd`.

## What the runs are

`scripts/bench.py` builds a deterministic synthetic series (`backtester.synthetic`). It is not a market download and it is not committed. `--scale ci` is 20 sessions. `--scale year` is 252 sessions. `--scale full` is `23 * 252` sessions, weekdays from 2003-01-02, about 04:00–19:59, with an early close every 20th session. The perf workflow gates the `ci` scale only.

Peak RSS is the sum of `VmRSS` for the bench process and its descendants, sampled every 0.25 s. Peak PSS is the sum of `Pss` from `smaps_rollup`, so a shared mapping is counted once. On Windows both numbers are the process high-water RSS.

## Two machines

The profile below is `main` at `2b73448` on a different host, with real SPY. It is not a paired timing. The paired tables are the next section: one host, synthetic `SYN`, this branch before the columnar store and this branch after it. The per-bar jail timings in that section are this branch against `main` at `2b73448` on that same host.

| | Profile of `2b73448` | Paired host |
| --- | --- | --- |
| OS | Linux 6.12, 8 vCPU | Linux 6.12.113+, 4 vCPU, 16 GB |
| Python | 3.13.5 | 3.12.3 |
| pandas / numpy | 3.0.1 / 2.5.4 | 3.1.1 / 2.5.4 |
| bubblewrap | 0.12.0 | 0.9.0 |
| Data | real SPY filled files | synthetic `SYN` |

Profile figures for real SPY, on the profile host:

| | 1 year (239,625 rows) | 2 years | 5 years (1,205,815 rows) |
| --- | --- | --- | --- |
| `load_named` | 4.74 s | 15.5 s | 87.6 s |
| of which the per-session volume loop | ~2.5 s | ~9.7 s | ~60 s |
| Arena SMA in-process, including load | 5.47–5.86 s, 352 MB | | |
| Arena SMA jailed | 10.95–10.99 s, 361 MB | | |
| Desk SMA in-process, including load | 9.01–9.47 s, 587 MB | | |
| Desk SMA jailed | 23.44–24.42 s, 648 MB | | |

The volume loop was one mask over every row for every session. A fit of that loop on the full SPY range (5,279,129 rows, 5,726 sessions) was about 28 minutes, and the in-process Arena RSS was extrapolated to about 5.9 GB. That range was not run. Jail overhead on that host fitted 0.31 s plus 42.6 µs per Arena RTH bar, and 0.24 s plus 56.7 µs per Desk bar. A direct pipe round trip was 45.7 µs, against a bare JSON ping-pong of 24 µs. Parallel Arena on SPY 2023, 8 strategies: 48.09 s at 1 worker (869 MB) to 14.78 s at 8 workers (2,666 MB).

## This host, paired

Linux 6.12.113+, 4 vCPU, 16 GB `MemTotal`, Python 3.12.3, pandas 3.1.1, numpy 2.5.4, bubblewrap 0.9.0. Synthetic `SYN`, 5,443,069 rows, 5,796 sessions, 267 monthly gzip files. Each phase is a fresh process. The before column is the earlier run on this host, while the prepared frame still materialized string columns and Desk kept one dict per bar. The after column is the columnar store (`SCHEMA` 2) measured later the same day.

### 23 years

In-process times are the engine on a frame that was already loaded. The before jailed times are `run_named` with a cold cache, so they include the parse. The after jailed times are `run_named` with `with_benchmarks` false on the cache the cold load had just written.

| | before s | before RSS | before PSS | after s | after RSS | after PSS |
| --- | --- | --- | --- | --- | --- | --- |
| cold load | 31.229 | 4,830 MB | 4,835 MB | 12.786 | 1,573.3 MB | 1,570.4 MB |
| warm load | 4.798 | 2,096 MB | 2,094 MB | 1.060 | 100.5 MB | 97.6 MB |
| Arena, one book, frame resident | 4.526 | 3,782 MB | 3,779 MB | 13.015 | 981.1 MB | 978.1 MB |
| Desk, one book, frame resident | 34.711 | 11,442 MB | 11,444 MB | 25.086 | 575.8 MB | 572.9 MB |
| Arena jailed | 148.511 | 6,547 MB | 6,502 MB | 87.230 | 1,084.6 MB | 1,040.5 MB |
| Desk jailed | 352.502 | 13,842 MB | 13,809 MB | 213.021 | 679.5 MB | 635.3 MB |

The warm-load RSS of 100.5 MB is the mapping before the columns are faulted. The book rows are the working set: Arena 978 MB PSS, Desk 573 MB PSS, jailed Arena 1,040 MB, jailed Desk 635 MB. A cold jailed run parses first; that parse peaks at the cold-load row (1,570 MB PSS) and the book then sits at the jailed row.

Arena in-process takes 13.0 s where the earlier run took 4.5 s. Each delivered bar is built from the columns at the call. The resident set for that book is 981 MB. Desk in-process is faster (34.7 s to 25.1 s) and the resident set is 576 MB.

Both Arena equities from the after run are `6788838.211430726`. Both Desk equities are `6788174.206709286`. Those match the earlier run on this host. They are synthetic prices.

Generating the 267 files took 72.824 s in the earlier run. That step only writes the gzip files.

### Parallel, 23 years

Eight Arena strategy files, one named market, one book per strategy, `with_benchmarks` false, warm cache, fresh process per worker count. Every job succeeded (`jobs` 8, no failures). `--scale full` stops at 4 workers.

| workers | seconds | vs 1 worker | peak RSS | peak PSS |
| --- | --- | --- | --- | --- |
| 1 | 679.318 | 1.00 | 1,137.4 MB | 1,092.7 MB |
| 2 | 361.066 | 1.88 | 2,144.7 MB | 1,533.8 MB |
| 4 | 162.573 | 4.18 | 4,149.8 MB | 2,406.0 MB |

PSS is the total. RSS adds `VmRSS` across the parent and the workers, so a page mapped by four processes is counted four times. Four workers share one artifact: 2.4 GB PSS, 4.1 GB of summed RSS.

### Per-bar jail, against `main` `2b73448`

Same host, same interpreter, `PYTHONPATH` selecting `/tmp/main-2b73448` for the main column. Three repeats. The slope is the median of 20,000 bars minus the median of 5,000, divided by 15,000.

Direct pipe. The strategy returns `None`. Main writes newline JSON. This branch writes a compact struct for the engine's bar and reads one decision before the next bar.

| | median 5,000 | median 20,000 | µs/bar |
| --- | --- | --- | --- |
| `2b73448` newline JSON | 0.198951 s | 0.788514 s | 39.304 |
| this branch, compact bar | 0.132630 s | 0.524960 s | 26.155 |

End-to-end `run_named`, `with_benchmarks` false. Net is the jailed run minus a separate warm load of that series. 20 sessions are 18,781 rows and 7,621 RTH bars. 60 sessions are 56,343 rows and 22,863 RTH bars. Arena delivers the RTH bars (15,242 more bars). Desk delivers every bar (37,562 more bars).

| | warm 20 | warm 60 | net 20 | net 60 | slope |
| --- | --- | --- | --- | --- | --- |
| `2b73448` Arena | 0.140 s | 0.574 s | 0.910 s | 1.444 s | 35.03 µs/RTH bar |
| this branch, Arena | 0.008 s | 0.015 s | 0.875 s | 1.213 s | 22.18 µs/RTH bar |
| `2b73448` Desk | 0.140 s | 0.574 s | 1.285 s | 3.285 s | 53.25 µs/bar |
| this branch, Desk | 0.008 s | 0.015 s | 1.071 s | 2.551 s | 39.40 µs/bar |

The slope is `(net 60 − net 20)` over the extra bars. Warm load on this branch is the prepared mapping. Warm load on `2b73448` parses the files again.

### One year and the CI scale, earlier run

These two tables are the earlier run on this host, before session and time were integer codes. They are the before side of a one-year batch. The 23-year table above is the after side.

One year is 252 sessions, 236,892 rows.

| | seconds | peak RSS | peak PSS |
| --- | --- | --- | --- |
| generate | 3.429 | 107 MB | 104 MB |
| cold load | 1.361 | 273 MB | 270 MB |
| warm load | 0.181 | 205 MB | 202 MB |
| Arena, one book | 0.144 | 239 MB | 236 MB |
| Desk, one book | 1.398 | 512 MB | 509 MB |
| Arena walk-forward | 8.742 | 474 MB | 429 MB |
| Arena jailed | 5.592 | 601 MB | 556 MB |
| Desk jailed | 13.275 | 905 MB | 861 MB |

Parallel Arena, eight strategy files, same year. This machine has four CPUs, so eight workers oversubscribe.

| workers | seconds | speedup vs 1 | peak RSS | peak PSS |
| --- | --- | --- | --- | --- |
| 1 | 40.169 | 1.00 | 529 MB | 482 MB |
| 2 | 19.834 | 2.03 | 781 MB | 669 MB |
| 4 | 9.801 | 4.10 | 1,279 MB | 1,039 MB |
| 8 | 11.316 | 3.55 | 2,259 MB | 1,761 MB |

On that earlier run, wall time scaled with the four cores and eight workers were slower than four. PSS grew because each worker's string columns were a private copy. The 23-year after table is the shared-code measurement.

CI scale (20 sessions, 18,781 rows), same earlier run: cold load 0.122 s, warm load 0.016 s, Arena 0.013 s, Desk 0.105 s, walk-forward 1.6 s, jailed Arena 0.84 s, jailed Desk 1.407 s. Parallel Arena with eight strategies: 5.914 s, 3.403 s, 2.007 s, 2.022 s at 1, 2, 4, and 8 workers. The gate passed.

## Design

### Loader

`load_named` hashes the gzip bytes and opens a prepared entry when the hash, `LOADER_VERSION` (2), and the load parameters match. On a miss it parses one month, copies the numeric columns out, and drops that month before the next file. The months are concatenated as arrays. A stamp of the shape `YYYY-MM-DDTHH:MM:SS±HH:MM` is split in NumPy into year, month, day, minute, and offset. Any other shape returns to the row-wise parser, so the existing error text stays. The per-session volume total is `factorize` plus `bincount`. Session volume, the short-session flag, and the golden frame values match the previous loader (`tests/golden_phase9.json`, frame `5aa55b96e3bc4c7c7a6566f7f46e7a24f3ea9132d424c3b7be69e893c7489a3d`).

### Prepared store

`backtester.prepared` writes a directory of `.npy` columns and a JSON manifest. `SCHEMA` is 2, and the schema is part of the cache key, so an older entry misses and is rebuilt. The format is NumPy because that is already in the installer. Arrow is not a dependency. The directory name is a 64-character BLAKE2b. A named key covers the schema, the loader version, the symbol, the bar size, the price kind, the phase map, and the hash of each source file. A window or a designer view is a different key: the kind is part of the hash, and the hash is of the column bytes, so a design window does not contain the locked bars.

Writes go to `.partial-<key>-<pid>-<ns>`, fsync the files and the directory, then `rename`. A reader checks the BLAKE2b of each file and the row count. A mismatch deletes the directory and the caller rebuilds it. A lock file is `O_EXCL`; a lock older than 15 minutes is stolen. A cache write that fails is recorded and the run keeps the frame it already built.

The directory is `STRATEGY_BUILDER_PREPARED_DIR`, or `%LOCALAPPDATA%\strategy-builder\prepared` on Windows, or `$XDG_CACHE_HOME/strategy-builder/prepared` otherwise. The cap is `STRATEGY_BUILDER_PREPARED_MAX_BYTES`, default 8 GiB. Eviction is oldest access first, and the entry just written is kept even when it alone exceeds the cap.

Numeric columns are mapped `mmap_mode="c"` (copy-on-write). A categorical column is integer codes plus the category labels. A stamp column is five integer arrays (year, month, day, minute, offset minutes). A constant symbol is one string in the manifest. Workers that map the same artifact share those pages. The public values match the pre-cache frame. Dtypes for the coded columns are categorical and stamp, which is what the golden frame hash locks.

Designer artifacts are screened before the write. The error is one fixed sentence. It does not include the ticker, the date, or the cell. The manifest does not store a source path, a scale, a date map, a sidecar, or the real symbol. `prepared list` labels those rows `RANDOM`. The pack's `date` column of `S#####` session ids is not an operator column. An ISO date in a text column is refused. The jail's deny list includes the cache directory, so a strategy process cannot open a neighbor's entry. The book worker, which is outside the jail, maps the one artifact for that job and then streams bars.

`python -m backtester prepared list|delete|rebuild` uses the same directory. Rebuild is for a named entry, from the source path stored on that manifest. The Data page lists the same rows and can delete or rebuild the selection.

A named parallel job passes the named artifact path to each worker and drops the parent's frame once that path is recorded. The worker maps the file. After the child exits, the parent maps it again to write the report. `with_benchmarks` false skips that second map. An anonymized job stores a screened designer artifact instead, and a designer child refuses an operator manifest. Walk-forward still gives the design child only the design slice. The locked window stays in the sealed spill until that child has exited. The parent deletes the full frame before the design child starts.

### Desk

`prepare_desk_bars` builds one columnar table (`session` id, phase, minute, OHLC, volume) and `engine_desk.run` walks it with a single reused row. The strategy still reads fields by name. `run_books`, the parallel batch, and the referee reuse that table across books. A caller that passes plain dicts still goes through `normalize_bars`. The 23-year in-process book is 573 MB PSS.

### Jail

The parent writes one bar and reads one decision before it writes the next. A bar with the engine's field set is a little-endian length and a compact struct (`bar_ipc.pack_bar`). Hello, state, day-end, and any other payload stay length-prefixed JSON. The child writes one decision, also a compact struct when the target is a scalar the engine accepts. On Linux the calling thread polls the pipe. There is no stdout helper thread. Windows keeps its reader threads. The child loads `bar_ipc.py` from the file beside it, because `python -I` does not put the script directory on `sys.path`. The frozen smoke allow-list includes that file.

A named jailed run drops the parent's frame while the book worker maps the artifact, then maps it again for the report. The strategy jail does not receive the prepared directory and does not inherit the memmap handles (`close_fds`, and the Windows handle list is the three stdio handles).

A relative `--strategy` path is made absolute in the parent, before a worker changes directory. A virtualenv whose prefix sits strictly inside a denied directory is mounted on its own; the rest of that directory is not. A prefix that is the denied directory itself is refused with the sentence that tells the operator to create `.venv` inside the clone or to use an interpreter outside it.

## Equivalence

`tests/test_phase9_equiv.py` replays a 20-session synthetic series and compares every report file with `tests/golden_main_2b73448.json`, captured from `main` at `2b7344819493ad0e204bc827ed355f32a6d06d10` on this host (CPython 3.12, NumPy 2.5.4). The cases are Arena and Desk, in-process and jailed, single book, walk-forward, pack, walk-pack, and parallel (two workers, and one worker with benchmarks off). `sidecar.json` drops `factor_key`, `factor_mac`, and `source_root`. `run.json` drops `id`. PNG bytes are locked on Linux. Windows checks the PNG signature. Text reports are hashed after CRLF is folded to LF, because the writers use the platform newline; the writers themselves are unchanged. Packs raise `unsafe-in-process cannot run a pack` for `--unsafe-in-process`, matching `main`.

NumPy 2.4.6 (CPython 3.11) and NumPy 2.5 disagree by one unit in the last place on a few Desk reductions: average exposure, turnover per year, annualized volatility, and the Sharpe ratio that divides by that volatility. The same figures are copied into the buy-and-hold and 2× benchmark files, the walk-forward summary, and the parallel leaderboard. `numpy24` in the golden file is the hash `main` wrote for each of those files on NumPy 2.4.6, checked on this host against a second tree from `main` at the same commit. Equity, fills, and every other report file are one hash on both NumPy builds. The comparison accepts a file when its hash is the NumPy 2.5 hash or that NumPy 2.4 hash.

`test_cold_warm_and_uncached_reports_match` runs the in-process Arena book three ways: empty cache, warm cache, and with `lookup_named` / `save_named` refusing. All three match the in-process golden. `test_jailed_cache_paths_match_main` does the same three for the jailed Arena book. `tests/golden_phase9.json` locks the loader frame hash, the inspection, and the Arena and Desk equity on the 3-session series.

## CI

`.github/workflows/perf.yml` runs `python scripts/bench.py --scale ci --gate` on Ubuntu (with bubblewrap) and Windows, Python 3.12. The gate is 60 s cold, 30 s warm, 60 s Arena, 90 s Desk, 180 s jailed Arena, 240 s jailed Desk. Parallel and walk-forward are printed and not gated. `tests/test_phase9_perf.py`, `tests/test_phase9_equiv.py`, and `tests/test_phase9_review.py` are on the fast shard.

## Limits

A single 23-year run on this host stays under 1.6 GB PSS: cold load 1,570 MB, Arena in-process 978 MB, Desk in-process 573 MB, jailed Arena 1,040 MB, jailed Desk 635 MB. Four Arena workers of that series are 2.4 GB PSS. Summed RSS at four workers is 4.1 GB because each worker counts the shared file.

Arena in-process wall time on this series is 13.0 s. The earlier columnar-string run was 4.5 s and 3.8 GB. The bytes moved into the per-bar call.

A jailed strategy cannot read the cache. A class strategy is not jailed, and a class that already knows the cache path can read it. That is the same boundary as the rest of the in-process opt-in.
