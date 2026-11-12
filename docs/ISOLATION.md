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
- **Dividends.** The parent resolves one accounting record from the full
  dividend file, then passes each child that record and only the ex-dates
  whose session is in that child's frame. An empty map means the file was
  present and this window has no ex-date. The child does not decide the
  method from the map. A locked ex-date and its amount are not in the design
  process. A dividend date that is not in the full sample fails the
  walk-forward for Arena and for Desk before either child starts. An Arena
  `run` of a complete sample still rejects a date that is not in that sample.
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
  system-directory variables, `TEMP`/`TMP`/`TMPDIR`, `PYTHONHOME`,
  `PYTHONNOUSERSITE`, `PYTHONHASHSEED`, `PYTHONUTF8`, `PYTHONIOENCODING`,
  `PYTHONUNBUFFERED`, `PYTHONSAFEPATH`, `PYTHONLEGACYWINDOWSSTDIO`,
  `PYTHONMALLOC`, `VIRTUAL_ENV`, the dynamic-linker path variables,
  `LANG`/`LC_*`, `TZ`, and on Windows `LOCALAPPDATA`). `PYTHONPATH` is not
  on that list. `LOCALAPPDATA` is the host path the AppContainer profile
  needs. The jail denies that directory. Every other
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

An anonymized pack run uses the same channels twice. `run_pack` reads the
sidecar in the operator process, checks the operator HMAC, then drops that
document before either spawn. The sidecar stores the scale factors, the date
map, the ticker, `factor_key`, and `factor_mac`.

The operator worker receives `share_scale` so per-share fees use real share
counts. It does not receive the sidecar document, `factor_key`, `factor_mac`,
the date map, the ticker, the volume factor, or the operator note. The
designer worker is a second process. It is not given `share_scale`,
`share_factor`, `factor_key`, `factor_mac`, or the operator account size.
Fills use the flat fee in `docs/COSTS_DESIGNER.md`. Equity starts at the
canonical index. Dividend cash per pack share is the file amount times the
price factor whose HMAC matched, never `share_scale`. When that worker runs
Arena, the engine unbinds `share_scale` before the bar loop. When it runs
Desk, the engine drops `share_factor` from the config before `on_bar`.

A strategy file does not run in either worker, and the operator process does
not import it. `run`, `run --pack`, `walkforward`, `parallel`, and `referee`
pass the path through. The worker launches the same jail as `backtester
referee`. The pipe is not given the sidecar, `factor_key`, `factor_mac`, the
scale, the accounting record, or the dividend map. The strategy process has
no engine frame. An in-memory class passed to the library still runs beside
the engine. `--unsafe-in-process` prints a warning and refuses a pack.
Benchmarks stay in the worker. The designer per-call timeout defaults to 10
seconds, the same as the referee, and `--call-timeout` changes it.

Post-fee cash on the operator book can still be read against the public
per-share schedule. That book is not the designer tree. The designer book
has no per-share term, and the flat constant is not derived from the ticker
or the pack.

The same pack leaks that `docs/PACK.md` lists apply to the bars themselves:
weekday plus early-close shape, a crash-shaped path, native tick times
`price_factor`, and bar `count`, which is not volume-scaled and is an
identity cue. `count` stays the source trade count. The billing-scale residual
does not apply to the designer child.

Heap, environment, argv, and cwd are the in-scope checks. A hardcoded absolute
path can still open a file. That is the Phase 4 jail.

File-backed design, locked, pack, and parallel runs use that jail. The reads
below fail in the strategy process:

- `/proc/<ppid>/cmdline`
- `/proc/<ppid>/environ`
- `/proc/<ppid>/cwd`, and the Windows handle equivalents
- `share_scale` on the operator worker's engine frame. The parent passes it
  so the real invoice uses real share counts. The designer worker does not
  receive it. The jailed strategy receives neither the scale, the sidecar,
  `factor_key`, nor `factor_mac`. See `docs/PACK.md` and
  `docs/COSTS_DESIGNER.md`.

`walk_forward` deletes the frame it was given. The command line passes that
frame as the argument and does not keep another name for it. A caller that
keeps its own variable still has that object. The design child still does not
receive it.

## Parallel runner

`backtester parallel` scores each strategy in a new spawned process. An
anonymized job spawns the designer worker first (flat fee, canonical equity,
no billing scale, dividend cash per pack share restated by the MAC-checked
price factor) and the operator worker second. The operator worker receives
`share_scale`. A named job spawns once. The strategy file runs in the jail
inside that worker and is not given the sidecar, `factor_key`, `factor_mac`,
or the scale. Desk dividends are remapped in the parent before the worker
starts.

Spawn copies the parent environment at `Process.start`. A launch lock holds
the argv, cwd, and environment scrub so two jobs cannot snapshot a cleared
environment. The lock is not a sandbox. The worker scrubs argv and the
environment again after it starts, so a spawn that copied the operator
process does not keep them. It also points
`multiprocessing.process.ORIGINAL_DIR` at the empty temp directory and drops
the operator directory from `sys.path`. Install paths stay. `PYTHONPATH` is
not copied into the child. If the parent had that variable, any entry that is
not the installed `backtester` package is also removed from the child's
`sys.path`. A directory that only holds another job's `metrics.json` is one
of those entries.

The worker calls `setsid` on Linux before it waits for the parent, so it is
its own process-group leader and the group id is the worker pid. A file
strategy is launched in the jail before the worker installs its seccomp
filter, because bubblewrap needs `setsid` for its own session and would
inherit the denial. The filter is then installed in the worker. The jail
has its own filter, which denies `setsid`, `setpgid`, and `fork`. A
grandchild the worker itself starts cannot call `setsid`. The parent
records that id when the child reports it has entered. A normal return, a
strategy exception, a timeout, a cancel, a resident-set kill, and the final
cleanup all signal that saved group: SIGTERM, then SIGKILL. The signal does
not ask `getpgid` again, so it still works after the leader has been reaped.
A grandchild that ignores SIGTERM dies on the SIGKILL. On Windows
every worker is placed in a job object before the parent releases it. The job
sets `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Every exit path, including a
normal return, calls `TerminateJobObject` and then closes the handle. If the
job cannot be assigned, the book fails. It does not fall back to signaling
only the worker pid. A memory cap, when one was requested, is an additional
commit-charge ceiling. The resident-set poll is separate from a `MemoryError`
the strategy raises.

The worker redirects file descriptors 1 and 2 to a file in its temp directory,
capped at 256 KiB. Further output is discarded without blocking the writer.
A short scrubbed tail of that output can be attached to the operator error
row. It is not written into `designer/`.

A file strategy's own `print` is not a protocol line. The jail keeps the
decision pipe separate and writes prints to `stdio.log` in the strategy's
cwd, with the same 256 KiB cap, still draining after the file is full. On a
failure that log is copied into the worker's stdio file, so the operator
error can show it and the designer sentence does not. `backtester referee`
does not do this: there, extra stdout is a protocol disqualification.
`os._exit` in the strategy skips that log. The worker then exits with the
same status, and the row is a crash rather than a strategy error.

The child returns equity, fills, and the statistics it computed. The parent
recomputes those statistics from the returned result and runs the benchmarks
itself. The published metrics are the parent's. If the child's copy does not
match, the row fails. A strategy can still patch a cost constant while its
own `on_bar` runs, because that loop is in the child; the parent's check
catches a rewritten metric function and a rewritten benchmark. Moving the
engine loop into the parent is the Phase 4 one-bar referee.

A crash, a hang, or a resident-set overrun is one failed row. The other jobs
keep running. The Linux non-dumpable flag is not set on this path. `run_pack`
does not set it either. The OS jail and the one-bar referee are below.
The Windows job object on this path is the process tree and, when requested,
the commit-charge ceiling. It is not the AppContainer sandbox. The referee
uses that sandbox.

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
The referee jail makes that open fail. This phase does not.

### Probe: `/proc/<ppid>/mem`

The design strategy opens `/proc/<ppid>/mem` and searches the parent address
space for a locked-tail close. The Windows equivalent is `OpenProcess` and
`ReadProcessMemory`.

Phase 1 sets the Linux parent non-dumpable while the design child runs.
That blocks the ordinary same-user ptrace path and is not applied on Windows.
It is not a claim that every read of parent memory fails. Inside the referee
sandbox a read of `/proc/<ppid>/mem` returns no bytes, `process_vm_readv` and
`ptrace` return EPERM, and Windows `OpenProcess` with `PROCESS_VM_READ` fails.

### Probe: file-based arming of the locked run

The design strategy writes a flag file at a path it chooses. The locked
strategy reads that file and trades only when the flag is present. The two
runs do not share a process, so an in-memory flag does not carry over. They
are not confined to a filesystem. A path outside the empty working directory
still works, and the locked run can see a file the design run wrote.
The referee jail does not mount those directories, and on Windows the
AppContainer token is not granted them. A file the jailed strategy creates
does not survive the jail and is not a path the next run is given.

## Referee sandbox (Phase 4)

`backtester referee` runs the official engine in the parent and the strategy
in a child. One JSON line goes out, one decision comes back, and the next bar
is not written before that decision. The default per-call limit is 10
seconds. `describe_isolation()` is what this machine will apply, and
`referee.json` records the backend that was actually used. A launch that
cannot apply the jail raises `SandboxError` and does not start the strategy.

### Windows

The backend name is `job+appcontainer`.

- The process is created suspended, assigned to a job, then resumed. The job
  sets `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`
  of 1, `JOB_OBJECT_LIMIT_PROCESS_TIME`, `JOB_OBJECT_LIMIT_JOB_TIME`,
  `JOB_OBJECT_LIMIT_PROCESS_MEMORY`, and `JOB_OBJECT_LIMIT_JOB_MEMORY`.
- The token is an AppContainer profile with zero capabilities, so it has no
  network capability. A socket call fails with access denied (10013).
-   One AppContainer profile (`strategybuilderjail`) is created per machine.
  `icacls` grants read/execute on the Python runtime and the Windows
  directory once, traverse-only on the directories between those and the
  volume root. The volume root itself is not edited. Later launches see a
  marker and do not walk that tree again. Each launch grants the private
  temp directory only. An `icacls` invocation that does not return in 45
  seconds fails the launch. There is no per-launch recursive deny: the container
  token is not the user, so a directory that was not granted is not readable.
  A child whose token is not an AppContainer, or whose token has a
  capability, is not resumed. Loopback exemptions for this profile are
  removed. Inbound and outbound firewall rules block the package SID, and
  a connect-layer filter blocks it too, including loopback. The filter is
  added in a dynamic WFP session (`FWPM_SESSION_FLAG_DYNAMIC`) and the engine
  handle is kept for the life of the process. A null session would be static:
  each worker would add another filter, and the filters would stay until
  reboot. A dynamic session removes them when the process exits. Windows otherwise
  drops loopback in the receive layer and `connect` waits until it times out.
  A connect to a
  listening loopback server and to an external
  address both fail with access denied (10013). `LOCALAPPDATA` is the host
  value. CreateProcess returns 203 when it is missing, so a missing value
  refuses the launch. That directory is denied without inheritance, once,
  because the profile and the private temp live under it. The process
  environment is a sorted Unicode block. CreateProcess rejects an unsorted
  block. A frozen child is `StrategyBuilder.exe --sandbox-child` (or the
  console `backtester.exe`), not a system Python running a `.py` file.
- `OpenProcess` of the parent with `PROCESS_VM_READ` is denied by the
  AppContainer token.
- If the profile, the job, the assignment, or an ACL edit fails, the launch
  raises and the process is not resumed. There is no second, weaker backend.

### Linux with bubblewrap

This is the CI backend. The workflow installs `bubblewrap`. The launch is
`bwrap --unshare-all --unshare-user --disable-userns --die-with-parent
--new-session --cap-drop ALL`. The child is `python -I -S -B`. `sys.path`
starts with staged directories that contain only the packages this process
imported (numpy and pandas, in the parent's order, plus the libraries they
need), then the standard library. site-packages is covered inside the
namespace and only those package directories are bound back, so `*.pth`,
`*.egg-link`, and `direct_url.json` are not on the child's path. `/etc/ld.so.cache`
is bound read-only. Data and repository paths are not mounted.

A seccomp filter returns EPERM for `socket`, `connect`, `accept`, `accept4`,
`sendto`, `recvfrom`, `sendmsg`, `recvmsg`, `shutdown`, `bind`, `listen`,
`socketpair`, `fork`, `vfork`, `ptrace`, `setpgid`, `setsid`, `unshare`,
`setns`, `process_vm_readv`, `process_vm_writev`, `userfaultfd`,
`io_uring_setup`, `io_uring_enter`, `io_uring_register`, and `pidfd_getfd`.
The io_uring numbers are 425, 426, and 427. Those three are also the generic
aarch64 numbers, but the other numbers differ, so a machine that is not
x86_64 still refuses the launch instead of using a partial table. `clone` is
allowed only when `CLONE_THREAD` is set. `clone3` stays allowed: glibc creates
threads with it, and classic BPF cannot read the flag pointer. A `clone3`
task inherits the filter, the mount namespace, and the network namespace, and
it dies with the pid namespace when the referee exits. It cannot call
`setsid`. `RLIMIT_NPROC` is 64, so a `clone3` that starts another process
stops at that ceiling with EAGAIN.

The host `/proc` is not mounted. `os.getppid()` inside the jail is the
bubblewrap process in the new pid namespace, not the referee. Opening
`/proc/1/mem` can return a descriptor; reading it returns EIO and zero
bytes. `process_vm_readv` and `ptrace` return EPERM. `/proc/1/cmdline` is
visible and is why the child's command line carries no data path, no ticker,
and no scale. The private directory path is on that command line because it
is the child's own work directory.

`RLIMIT_AS`, `RLIMIT_DATA`, `RLIMIT_NPROC` (64), and `RLIMIT_CPU` are set
before the strategy is loaded. The parent also enforces the wall-clock
per-call timeout and kills the sandbox on the way out. `--die-with-parent`
covers a referee that dies without running that cleanup.

### Linux without bubblewrap

If `bwrap` cannot start a namespace, the launch uses landlock plus the same
seccomp filter, the same rlimits, and `PR_SET_PDEATHSIG` set to `SIGKILL`.
Landlock allows reading the interpreter, the staged package directories, and
the directory that holds `/etc/ld.so.cache`, and writing the private
directory. The child is still `python -I -S`, so site-packages is not on
`sys.path`. Opening a data path returns EPERM. There is no pid namespace and
no network namespace, so the network block is the seccomp filter alone,
including `io_uring_setup`. `/proc` is not in the allow-list. A raw `clone3`
task on this backend is not reaped by a pid namespace; bubblewrap is the
stronger backend and is the one CI installs.
If landlock cannot be applied either, the launch is refused.

### Open Windows items

These are the Windows-only cases left for a live check on the owner PC.
Linux runs the same assertions and does not take the `xfail`. The mark is
`xfail(strict=False, reason="verify live on owner Windows PC")`. A pass on
Windows is an xpass. Windows CI runs one shard per group of files, on
Python 3.11, 3.12, and 3.13. Each shard stops at 25 minutes and each test
stops at 60 seconds (`pytest-timeout`). A parallel shard holds at most 12
tests, so a shard ends in a pass or a fail instead of being cancelled.
The thread timer raises in that test instead of calling `os._exit`, so one
slow test does not discard the rest of the run. A hang cannot hold the job
for hours.

H3, network. AppContainer drops loopback in the receive layer, so `connect`
to a listening loopback server waits until the socket times out unless a
WFP connect-layer filter for the package SID is installed. `netsh` rejects
`package=` on the CI image. Firewall rules do not cover `127.0.0.1`. The
probe still requires access denied (`EPERM`, `EACCES`, or `10013`) on both
the listener and `1.1.1.1:443`. A refused or timed-out connect is not a
block. The installer smoke on `642f31f` (run 43125742983) saw that access
error from the frozen app. The unit probe has not been observed after that
filter. On `aa90226` the matrix still stopped at the first 60s pipe wait
(see H5), so this unit probe has not been reached on a finished Windows run.

- `tests/test_referee.py::test_selftest_probes_are_blocked[net.py-0.0]`
- `tests/test_designer_jail.py::test_absolute_read_arm_memory_and_network_stay_in_the_jail`
  (the network leg is this probe; the same test is also H5)

H5, time. On `0852f71` the Windows Test step took 2h 40m and 3h 15m.
Per-launch `icacls /T` was the cost, and `icacls` on
`C:\hostedtoolcache\windows` did not return (session start died at 180s).
Grants are once per machine now, and traverse uses the ACL API instead of
`icacls`. The sleep disqualification was 13.1s against an 8s wall-clock
bound while that grant ran inside the call. The parallel leg of the
designer-jail test hit 60s. The reserved benchmark publish uses a 90s
worker budget. The fast strategy in the hang test also came back as a
timeout when startup was slower than its 6s budget. On `aa90226` (push
run 43129181253) the matrix did not finish. Python 3.12 stopped in
`tests/test_accounting.py::test_walkforward_cli_keeps_a_file_when_the_design_window_has_no_ex_date`
while `_take_result` waited on the worker pipe. On `76e7e08` that test
failed at 60s on Python 3.11 and 3.13 of the pull-request run (it passed
on 3.12 only), and on all three versions of the push run. The traceback
was `runner.py` inside `_take_result` with the child still alive, on the
desk-raw call, after the arena and desk reports were already written.
`test_walkforward_cli_keeps_a_locked_ex_date_and_run_rejects_a_typo` and
`test_file_jail.py::test_cli_paths_do_not_import_the_strategy` failed the
same way on some of those jobs. None of those three is marked.

The wait was `multiprocessing.connection.wait` on the result pipe. On
Windows that posts a zero-byte overlapped read and cancels it. A result
larger than the 8 KiB message pipe stays stuck in `WriteFile` in the
child, so the parent loops in `WaitForMultipleObjects` with the child
alive. The Windows wait now peeks the pipe and does a real read, and it
waits only on the process sentinel. Every two seconds it writes
`walk-forward wait waited=… alive=… exit=… pipe_bytes=…` to the real
stderr. A timeout includes `alive` and `pipe_bytes`. Each Windows
`_walk_cli` call writes `walk-cli profile=… out=… seconds=… code=…`.
On `c0daa62` the peek saw no bytes. Completed calls wrote
`walk-cli profile=arena … seconds=23.937 code=0` and
`walk-cli profile=desk … seconds=27.203 code=0` on Python 3.11, and
`25.744` / `22.744` on Python 3.13. The 60s cap then fired on the next
call while `pipe_bytes=0` and the child was still alive, about 12s into
that call. Python 3.12 finished the combined tests inside 60s. The cap
was the sum of the calls. Each of those calls is now its own test. A
single call that stays at `pipe_bytes=0` with the child alive until 60s
stays red.

- `tests/test_referee.py::test_sleep_and_lookahead_are_disqualified_within_eight_seconds`
- `tests/test_designer_jail.py::test_absolute_read_arm_memory_and_network_stay_in_the_jail`
- `tests/test_reserved.py::test_a_passing_symbol_publishes_canonical_benchmark_names`
- `tests/test_parallel.py::test_a_hang_times_out_without_stopping_the_other_strategy`
- `tests/test_parallel.py::test_schema_token_tickers_publish_and_register`
  On `b9abba5` this file-backed pack exceeded 60s on Python 3.11, 3.12, and
  3.13. The other parallel tests in that run finished. The Windows matrix
  was then cancelled at the 25-minute job cap while pytest was printing the
  summary, so this was the only timeout recorded.

H7, numpy on Windows, not marked. On `844626d` the child aborted with
`OSError: [WinError 87] The parameter is incorrect: 'pydeps\00\numpy.libs'`
because `add_dll_directory` was given the junction. `20359e2` does not link
`*.libs` into the slot and calls `add_dll_directory` on the real directory.
On `aa90226` and again on `b9abba5` the Windows jailed-examples job passed
all 40 (D01–D20, arena and desk), including the numpy strategies. Those
examples stay strict on both runners. Ubuntu matches them jailed against
in-process, with `/etc/ld.so.cache` bound and the parent's import order on
`sys.path`. The frozen installer smoke on `b9abba5` (run 43131711177) completed.

`tests/test_referee.py` and `tests/test_reserved.py` are one of those shards,
with the same 60-second test cap and 25-minute job cap. Ubuntu still runs
the whole suite in one job per Python version.

### Live Windows checklist

From the repo root, with the package installed. These stay open for the
owner PC. The accounting and file-jail commands are not `xfail` items.

```
python -m pytest --runxfail -vv --timeout=180 --timeout-method=thread tests/test_referee.py::test_selftest_probes_are_blocked tests/test_referee.py::test_sleep_and_lookahead_are_disqualified_within_eight_seconds tests/test_designer_jail.py::test_absolute_read_arm_memory_and_network_stay_in_the_jail tests/test_reserved.py::test_a_passing_symbol_publishes_canonical_benchmark_names tests/test_parallel.py::test_a_hang_times_out_without_stopping_the_other_strategy tests/test_parallel.py::test_schema_token_tickers_publish_and_register
```

`--runxfail` makes a miss fail the process. Record the duration of the schema test. The CI cap that marked it was 60s.

```
python -m pytest -vv -s --timeout=180 --timeout-method=thread tests/test_accounting.py::test_walkforward_cli_keeps_a_file_when_the_design_window_has_no_ex_date tests/test_accounting.py::test_walkforward_cli_keeps_a_locked_ex_date_and_run_rejects_a_typo tests/test_accounting.py::test_run_rejects_a_dividend_date_outside_the_sample tests/test_file_jail.py::test_cli_paths_do_not_import_the_strategy
```

Watch whether a single walk-forward stays inside `_take_result` with the child alive. The desk-raw call was the one that sat there. The log lines are `walk-cli profile=…` and `walk-forward wait waited=… alive=… exit=… pipe_bytes=…`.

```
python -m pytest -q --timeout=120 --timeout-method=thread tests/test_referee.py tests/test_reserved.py tests/test_walkforward.py tests/test_reports.py tests/test_registry.py tests/test_paths.py tests/test_parity.py
```

```
python -m pytest -q -m jailed_sample --timeout=60 --timeout-method=thread tests/test_jailed_examples.py
```

After one jailed run, `netsh wfp show filters file=wfp.xml` and count filters named `strategybuilderjail block connect`. Run a second jail in the same process and see whether the count grows. Those filters should disappear when the process exits.

### What the child is not given

The bar and the account or state are the engine's public fields. Keys named
`share_scale` or `share_factor`, and any key that starts with `_`, are
dropped before the line is written. The calendar month callable and the
sidecar stay in the parent. The designer-facing ticker stand-in remains
`RANDOM`. Post-fee cash can still be used to infer a scale when someone runs
the engine themselves. The jailed strategy does not have that frame.

Arena warm-up and Desk warm-up are separate. Arena replays about the last
504 train sessions, then rescales to $1,000,000 keeping exposure. Desk
replays the whole train history with `live` false and a flat book, then
goes live at $1,000,000. A Desk run that is given `--warmup-days` is refused.
