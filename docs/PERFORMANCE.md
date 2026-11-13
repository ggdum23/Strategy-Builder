# Performance

A named series is parsed once and reused. Later runs, walk-forward windows, and parallel workers map that result. The engines, the fee schedules, and the one-bar jail protocol are unchanged: a strategy still sees bar `t` only after it has decided bar `t-1`, and it never receives the series as a file.

`costs.py` is `a3b0a522d8bf469001e75aaf274b0c5d3cbef624a68ed9fb94d29298110fe281`. `costs_desk.py` is `07b0d25ceeebe292f03a2b7b6ca0d76d76fa8d6a59f21d60103abfceeab0ebbd`.

## What the runs are

`scripts/bench.py` builds a deterministic synthetic series (`backtester.synthetic`). It is not a market download and it is not committed. `--scale ci` is 20 sessions. `--scale year` is 252 sessions. `--scale full` is `23 * 252` sessions, weekdays from 2003-01-02, about 04:00–19:59, with an early close every 20th session. The perf workflow gates the `ci` scale only. The limits are wide enough for a loaded runner and tight enough that the old per-session volume loop cannot pass.

Peak RSS is the sum of `VmRSS` for the bench process and its descendants. Peak PSS is the sum of `Pss` from `smaps_rollup`, so a shared mapping is counted once. On Windows both numbers are the process high-water RSS.

## Two machines

These are not paired timings. The "before" column is the profile of `main` at `2b73448` on a different host. The "after" column is this branch on the host that produced the tables below.

| | Profile of `2b73448` | This branch |
| --- | --- | --- |
| OS | Linux 6.12, 8 vCPU | Linux 6.12.113+, 4 vCPU |
| Python | 3.13.5 | 3.12.3 |
| pandas / numpy | 3.0.1 / 2.5.4 | 3.1.1 / 2.5.4 |
| bubblewrap | 0.12.0 | 0.9.0 |
| Data | real SPY filled files | synthetic `SYN` |

Profile figures for real SPY, same host as that profile:

| | 1 year (239,625 rows) | 2 years | 5 years (1,205,815 rows) |
| --- | --- | --- | --- |
| `load_named` | 4.74 s | 15.5 s | 87.6 s |
| of which the per-session volume loop | ~2.5 s | ~9.7 s | ~60 s |
| Arena SMA in-process, including load | 5.47–5.86 s, 352 MB | | |
| Arena SMA jailed | 10.95–10.99 s, 361 MB | | |
| Desk SMA in-process, including load | 9.01–9.47 s, 587 MB | | |
| Desk SMA jailed | 23.44–24.42 s, 648 MB | | |

The volume loop was one mask over every row for every session. A fit of that loop on the full SPY range (5,279,129 rows, 5,726 sessions) was about 28 minutes, and the in-process Arena RSS was extrapolated to about 5.9 GB. That range was not run. Jail overhead on that host fitted 0.31 s plus 42.6 µs per Arena RTH bar, and 0.24 s plus 56.7 µs per Desk bar. A direct pipe round trip was 45.7 µs, against a bare JSON ping-pong of 24 µs. Parallel Arena on SPY 2023, 8 strategies: 48.09 s at 1 worker (869 MB) to 14.78 s at 8 workers (2,666 MB).

## This host

Synthetic `SYN`. The 23-year set is 5,443,069 rows, 5,796 sessions, 267 monthly gzip files.

### 23 years

In-process times are the engine on a frame that was already loaded. The jailed times are `run_named` with a cold cache, so they include the parse.

| | seconds | peak RSS | peak PSS |
| --- | --- | --- | --- | --- |
| generate (files only) | 72.824 | | |
| cold load | 31.229 | 4,830 MB | 4,835 MB |
| warm load | 4.798 | 2,096 MB | 2,094 MB |
| Arena, one book, frame resident | 4.526 | 3,782 MB | 3,779 MB |
| Desk, one book, frame resident | 34.711 | 11,442 MB | 11,444 MB |
| Arena walk-forward, jailed file | 191.503 | | |
| Arena jailed, cold cache | 148.511 | 6,547 MB | 6,502 MB |
| Desk jailed, cold cache | 352.502 | 13,842 MB | 13,809 MB |

The two Arena equities from separate processes matched (`6788838.211430726`), and the two Desk equities matched (`6788174.206709286`). Those are synthetic prices. Walk-forward wall time is from a process that had already run the in-process books; its RSS is not in the table because that sample was not a fresh process.

A warm load of this series is a few seconds. A jailed Arena run of it is about two and a half minutes. A jailed Desk run is about six minutes. Arena resident set for one in-process book is 3.8 GB. Desk is not: the engine holds one plain dict per bar, and 5.4 million of those is about 11 GB on top of the frame the parent still has. The strategy process itself stays small (about 14 MB). The bytes are in the parent and the book worker.

### One year (252 sessions, 236,892 rows)

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

Wall time scales with the four cores. Eight workers are slower than four. PSS grows more slowly than RSS, which is the shared numeric mapping, and it still grows, because each worker's string columns are private.

### CI scale (20 sessions, 18,781 rows)

Cold load 0.122 s, warm load 0.016 s, Arena 0.013 s, Desk 0.105 s, walk-forward 1.6 s, jailed Arena 0.84 s, jailed Desk 1.407 s. Parallel Arena with eight strategies: 5.914 s, 3.403 s, 2.007 s, 2.022 s at 1, 2, 4, and 8 workers. The gate passed.

## Design

### Loader

`load_named` hashes the gzip bytes and opens a prepared entry when the hash, `LOADER_VERSION` (2), and the load parameters match. On a miss it parses every month, validates, and stores the columns.

The per-session volume total is `factorize` plus `bincount`. Non-finite checks walk each column once, in the same order as the old row scan, and the message still names that column. A stamp of the shape `YYYY-MM-DDTHH:MM:SS±HH:MM` is split in NumPy. Any other shape uses `datetime.fromisoformat`, so the existing error text stays. Session volume, the short-session flag, and the golden frame hash match the previous loader (`tests/golden_phase9.json`).

After the numeric columns have been copied out, a canonical parse drops the concatenated month frame and collects. On this host that cut the cold peak from 6.0 GB to the 4.8 GB in the table. The frame written to the cache is the one the caller receives.

### Prepared store

`backtester.prepared` writes a directory of `.npy` columns and a JSON manifest. The format is NumPy because that is already in the installer. Arrow is not a dependency. The directory name is a 64-character BLAKE2b. A named key covers the schema, the loader version, the symbol, the bar size, the price kind, the phase map, and the hash of each source file. A window or a designer view is a different key: the kind is part of the hash, and the hash is of the column bytes, so a design window does not contain the locked bars.

Writes go to `.partial-<key>-<pid>-<ns>`, fsync the files and the directory, then `rename`. A reader checks the BLAKE2b of each file and the row count. A mismatch deletes the directory and the caller rebuilds it. A lock file is `O_EXCL`; a lock older than 15 minutes is stolen. A cache write that fails is recorded and the run keeps the frame it already built.

The directory is `STRATEGY_BUILDER_PREPARED_DIR`, or `%LOCALAPPDATA%\strategy-builder\prepared` on Windows, or `$XDG_CACHE_HOME/strategy-builder/prepared` otherwise. The cap is `STRATEGY_BUILDER_PREPARED_MAX_BYTES`, default 8 GiB. Eviction is oldest access first, and the entry just written is kept even when it alone exceeds the cap.

Numeric columns are mapped `mmap_mode="c"` (copy-on-write). A second `load` of the same file is a second mapping of those pages. String columns are stored as fixed-width Unicode and returned as pandas `str`, which is a private copy. That copy is why a worker's RSS is not just a share of the parent's. The public frame's dtypes match the pre-cache frame, which is what the golden hash locks.

Designer artifacts are screened before the write. The error is one fixed sentence. It does not include the ticker, the date, or the cell. The manifest does not store a source path, a scale, a date map, a sidecar, or the real symbol. `prepared list` labels those rows `RANDOM`. The pack's `date` column of `S#####` session ids is not an operator column. An ISO date in a text column is refused. The jail's deny list includes the cache directory, so a strategy process cannot open a neighbor's entry. The book worker, which is outside the jail, maps the one artifact for that job and then streams bars.

`python -m backtester prepared list|delete|rebuild` uses the same directory. Rebuild is for a named entry, from the source path stored on that manifest. The Data page lists the same rows and can delete or rebuild the selection.

A named parallel job passes the named artifact path to each worker. It does not write a second full copy for the batch. An anonymized job stores a screened designer artifact instead, and a designer child refuses an operator manifest. Walk-forward still gives the design child only the design slice. The locked window stays in the sealed spill until that child has exited.

### Desk

`prepare_desk_bars` runs `frame_to_desk_bars` and `normalize_bars` once. `PreparedDeskBars` tells `engine_desk.run` to keep that tuple. `run_books`, the parallel batch, and the referee reuse it across books. The engine still reads the same fields. The memory table is the cost of one dict per bar: preparing once removes the repeats, and it does not shrink the one copy.

### Jail

The parent writes one length-prefixed JSON frame and reads one decision before it writes the next bar. On Linux the calling thread polls the pipe. There is no stdout helper thread. Windows keeps its reader threads. The child loads `bar_ipc.py` from the file beside it, because `python -I` does not put the script directory on `sys.path`. The frozen smoke allow-list includes that file.

The strategy jail does not receive the prepared directory and does not inherit the memmap handles (`close_fds`, and the Windows handle list is the three stdio handles). A 23-year jailed run leaves the strategy process at about 14 MB while the book worker holds the frame.

A relative `--strategy` path is made absolute in the parent, before a worker changes directory. A virtualenv whose prefix sits strictly inside a denied directory is mounted on its own; the rest of that directory is not. A prefix that is the denied directory itself is refused with the sentence that tells the operator to create `.venv` inside the clone or to use an interpreter outside it.

## CI

`.github/workflows/perf.yml` runs `python scripts/bench.py --scale ci --gate` on Ubuntu (with bubblewrap) and Windows, Python 3.12. The gate is 60 s cold, 30 s warm, 60 s Arena, 90 s Desk, 180 s jailed Arena, 240 s jailed Desk. Parallel and walk-forward are printed and not gated. `tests/test_phase9_perf.py` and `tests/test_phase9_review.py` are on the fast shard.

## Limits

Desk at 23 years is about 11 GB in-process and about 14 GB jailed, because both the book worker and the parent hold the series and the worker also holds a dict per bar. Arena in-process is 3.8 GB. Arena jailed is 6.5 GB: the parent keeps the frame for the metric check, and the worker builds its own string columns from the mapping.

Parallel workers share the numeric pages of one named artifact. They do not share the string columns. On this 4-core host, four workers of a one-year Arena batch are the fast case (9.8 s, 1.0 GB PSS). Eight workers are slower and larger.

A jailed strategy cannot read the cache. A class strategy is not jailed, and a class that already knows the cache path can read it. That is the same boundary as the rest of the in-process opt-in.
