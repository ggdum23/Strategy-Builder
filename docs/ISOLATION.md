# Walk-forward isolation (Phase 1)

Phase 1 runs the design book and the locked book in separate interpreters
started with `multiprocessing` spawn. A full OS jail is Phase 4: the Windows
referee sandbox (job object, restricted token or AppContainer, and file ACLs).
Phase 1 does not build that jail.

## What Phase 1 isolates

- **Heap.** Spawn does not copy the parent address space. Before the design
  child starts, the parent deletes the full frame and the locked slice and
  collects garbage. The design child is given the design frame only. The
  locked window is opened again only after that child has exited. The locked
  book is a different process, so class attributes and objects created in the
  design run are not visible to it.
- **Spill.** The locked window is written to a temporary file for that gap.
  The file is not an openable pickle. Its bytes are sealed with an
  HMAC-SHA256 keystream (stdlib `hmac` and `hashlib`). The 32-byte key is a
  local in `walk_forward` and is not put in the child job, the environment,
  argv, or the file. The file is still named `bt-locked-*.pkl` in the system
  temp directory, and `TEMP`/`TMP`/`TMPDIR` stay on the child's allowlist, so
  the child can read the file. A random name is not the protection: the
  plaintext close is not in the file.
- **Class attributes and module globals.** Each book imports the strategy in
  a fresh interpreter. A flag set on a class during the design run is not set
  in the locked run.
- **Environment.** The child receives an allowlist (`PATH`, the Windows
  system-directory variables, `TEMP`/`TMP`/`TMPDIR`, `PYTHON*`, `VIRTUAL_ENV`,
  the dynamic-linker path variables, `LANG`/`LC_*`, and `TZ`). Every other
  variable is dropped, including a data-root variable. `PATH` is kept because
  the interpreter and native libraries may need it. A data directory that
  someone has placed on `PATH` is not removed.
- **argv.** Spawn copies `sys.argv` when the process starts. The parent sets
  `sys.argv` to `['backtester-design']` before `Process.start` and restores
  its own argv afterwards. The worker sets the same argv again before the
  strategy is loaded or `on_bar` runs.
- **cwd.** The child starts in a fresh empty temporary directory. The parent
  changes to that directory only for the `start` call, then restores the
  caller's working directory. The worker changes to the same directory again
  before the strategy runs.
- **Linux dumpable flag.** For the design child's lifetime the parent calls
  `prctl(PR_SET_DUMPABLE, 0)` and restores the previous value when that child
  exits. A non-dumpable process cannot be attached with ptrace by the same
  user. This is not done on Windows, and it is not a jail.

The design child also receives the strategy (the class, or the strategy file
path when the class was loaded from a file) and the run kwargs. It does not
receive the data directory, the CLI argv, or the locked frame.

An anonymized pack run uses the same channels. `run_pack` reads the sidecar in
the operator process, then drops that document before the spawn. The child
receives the anonymized frame and the numeric `share_factor` (the price factor
k, which the engine needs in order to bill real share counts). It does not
receive the sidecar document, the date map, the ticker, the volume factor, the
operator note, or the sidecar path. Heap, environment, argv, and cwd are the
in-scope checks. A hardcoded absolute path can still open a file; that is the
Phase 4 jail, not this one.

`walk_forward` deletes the frame it was given. The command line passes that
frame as the argument and does not keep another name for it. A caller that
keeps its own variable still has that object. The design child still does not
receive it.

## What Phase 1 does not isolate

These three probes are Phase 4 acceptance tests. Phase 1 does not stop them.
Do not treat a passing Phase 1 suite as evidence that they fail.

### Probe: `follow.py` opens the data file

A strategy reads the source directory from `sys.argv` (the `--src` path the
operator passed) or opens a hardcoded absolute path, and reads monthly files
the design window must not have.

Phase 1 leaves argv as `['backtester-design']`, the working directory as an
empty temporary directory, and data-path environment variables unset, so the
argv form does not see the source. A hardcoded absolute path still opens.
Phase 4 must make both opens fail.

### Probe: `/proc/<ppid>/mem`

The design strategy opens `/proc/<ppid>/mem` and searches the parent address
space for a locked-tail close. The Windows equivalent is `OpenProcess` and
`ReadProcessMemory`.

Phase 1 sets the Linux parent non-dumpable while the design child runs.
That blocks the ordinary same-user ptrace path and is not applied on Windows.
It is not a claim that every read of parent memory fails. Phase 4 must make
the `/proc/<ppid>/mem` read and the `OpenProcess` read fail inside the
referee sandbox.

### Probe: file-based arming of the locked run

The design strategy writes a flag file at a path it chooses. The locked
strategy reads that file and trades only when the flag is present. The two
runs do not share a process, so an in-memory flag does not carry over. They
are not confined to a filesystem. A path outside the empty working directory
still works, and the locked run can see a file the design run wrote.
Phase 4 must stop the design run from creating a file the locked run can read.
