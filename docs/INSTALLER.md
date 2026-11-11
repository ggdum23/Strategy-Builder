# Windows installer

Strategy Builder ships as a per-user `setup.exe`. The installer is built on
the `installer` GitHub Actions workflow (`windows-latest`) with PyInstaller
and Inno Setup. The installed program carries its own Python. It does not
use the system interpreter.

The desktop window is a separate phase. Until that package exists, the Start
Menu shortcut opens a short message that points at `backtester.exe`.

## What gets installed

The PyInstaller build is **one folder**, not one file. Both programs sit in
the same directory and share `_internal`:

| File | Role |
| --- | --- |
| `backtester.exe` | Console CLI. Same commands as `python -m backtester`. |
| `Strategy Builder.exe` | Windowed launcher. No console window. |
| `defaults.json` | Data-folder default chosen at install time. |
| `README.md`, `LICENSE.txt`, `INSTALLER.md` | The copies shown by setup. |

`backtester.exe --version` and `backtester.exe version` print the package
version. `backtester test` still needs a source checkout; it is not part of
the frozen program.

User data is not installed under Program Files or the per-user Programs
directory. It lives in `%LOCALAPPDATA%\StrategyBuilder`:

| Path | Contents |
| --- | --- |
| `config.json` | `data_root`, `runs_dir`, `registry_path`, `install_dir` |
| `registry.json` | Series registry, when you point `--registry` here |
| `runs\` | Suggested directory for `--out` |

Uninstall removes the program files and the Start Menu entries. It leaves
`%LOCALAPPDATA%\StrategyBuilder` in place. The uninstaller asks before
deleting that folder. A silent uninstall does not delete it.
`/DELETEUSERDATA` on the uninstaller does.

The market-data folder is only a path in `config.json`. Setup does not
create it and does not copy, move, or unzip files into it.

## Install

Default is the current user, with no administrator prompt. The wizard can
switch to all users (`/ALLUSERS` from a command line). All-users mode does
not write into one person's profile; the first launch of either executable
creates that user's config from `defaults.json`.

```text
StrategyBuilder-Setup-0.1.0.exe /VERYSILENT /CURRENTUSER /DIR="D:\Strategy Builder"
```

`/DATA=` overrides the market-data folder on a silent install, for example
`/DATA=D:\market`. The folder is not created.

The wizard's suggested data folder is `C:\Users\Jeu\Desktop\Backtester`.
That string is the installer default. Application code reads it from
`config.json` or `defaults.json` and does not hardcode it.

Shortcuts: a Start Menu folder (Strategy Builder, the command line, and
uninstall) and an optional desktop shortcut. The desktop shortcut is
unchecked by default, so a silent install does not create one. Add
`/MERGETASKS=desktopicon` when you want it. Silent installs do not launch
the window at the end.

## Build

From a Windows checkout, with Python 3.12:

```powershell
py -3 -m pip install .
py -3 -m pip install -r packaging\requirements-build.txt
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

The script installs Inno Setup 6.7.4 if `ISCC.exe` is not already on the
machine. Output:

```text
packaging\dist\StrategyBuilder\          the one-folder bundle
packaging\dist\StrategyBuilder-Setup-<version>.exe
packaging\dist\SHA256SUMS
```

`packaging\requirements-build.txt` pins PyInstaller. The application
dependencies come from `pyproject.toml`. When that file defines a `gui`
extra, the build installs it before freezing, so PySide6 and pyqtgraph are
picked up without a spec edit.

`SOURCE_DATE_EPOCH` is the current commit timestamp and `PYTHONHASHSEED` is
`0`. PyInstaller honors the epoch for archive timestamps. Inno Setup still
writes its own compiler timestamp into `setup.exe`, so two builds of the
same commit are not guaranteed to be byte-identical. The workflow uses the
latest Python 3.12 patch and the version floors in `pyproject.toml`, so
wheel updates also change the hash. The workflow prints the SHA256 of the
file it actually produced.

Code signing is off. The build does not read a certificate or a repository
secret. To sign a later build, set these for that process and re-run it:

| Variable | Meaning |
| --- | --- |
| `SIGN_PFX_PATH` | Path to the code-signing certificate (`.pfx`). Not committed. |
| `SIGN_PFX_PASSWORD` | Password for that certificate. |
| `SIGN_TIMESTAMP_URL` | Optional RFC 3161 timestamp server. The default is DigiCert's. |

`packaging\sign_setup.ps1` uses `signtool sign /fd SHA256 /td SHA256`.
With `SIGN_PFX_PATH` unset it prints `code signing skipped` and leaves the
file unsigned. Put the certificate in a GitHub Actions secret and call the
script from the workflow only after that secret exists. Do not commit the
`.pfx`.

## CI

`.github/workflows/installer.yml` on `windows-latest`:

1. Build the bundle and `setup.exe`.
2. Print the SHA256, size, and file name.
3. Silent per-user install (`/VERYSILENT /CURRENTUSER`) into a directory
   whose path contains a space.
4. Smoke-test the installed `backtester.exe`: `--version`, ingest, a short
   arena run, and a walk-forward that spawns two children.
5. Silent uninstall, then check that the user-data folder is still there.
6. Upload `strategy-builder-setup` (the setup executable and `SHA256SUMS`).

A tag named `v*` attaches those files to the GitHub Release. The sample
month used by the smoke test is the checkout's `assets/sample` tree (or
`sample_data` if that directory is added later). It is not inside the
installer.

## Frozen multiprocessing

Walk-forward starts each book with `multiprocessing.get_context("spawn")`.
In a frozen program the child is a new copy of the same executable, with
`--multiprocessing-fork` on its command line. `packaging/frozen_entry.py`
calls `freeze_support` before it imports the application or looks at the
executable name. PyInstaller's runtime hook has already replaced that
function so the resource tracker is diverted too.

The runner's own scrub is unchanged and still applies in the child:

- argv becomes `['backtester-design']` before the strategy is loaded
- the working directory is an empty `bt-book-*` directory
- the environment keeps only the allowlist in `runner._ENV_KEEP`

`PATH`, `SYSTEMROOT`, `TEMP` / `TMP`, and `LD_LIBRARY_PATH` stay, because
the frozen child has to load its own DLLs. `LOCALAPPDATA`, `USERPROFILE`,
`APPDATA`, and any data-root variable do not. A strategy that needs a
writable cache should use `TEMP`.

`sys.executable` inside the bundle is `backtester.exe` or
`Strategy Builder.exe`. It is not a Python interpreter. Do not spawn
`sys.executable -m backtester`. A windowed process that wants a console
child launches the sibling:

```python
from pathlib import Path
import sys

exe = Path(sys.executable)
console = exe.with_name("backtester.exe" if exe.suffix.lower() == ".exe" else "backtester")
```

The same helper is `strategy_builder_config.console_executable` when
`packaging/` is on the path, which it is inside the frozen bundle.

## What the next phases must keep working

The spec collects optional libraries from whatever is installed in the
build environment. New backtester modules are collected by
`collect_submodules("backtester")` as long as they are real importable
modules under that package.

**Parallel runner and reports**

- Import matplotlib normally. The Agg and PDF backends and `mpl-data` are
  collected. Headless PNG output does not need a Qt backend.
- Spawn workers with the standard library `multiprocessing` spawn context
  (or a pool built on it). Other frameworks are not supported in the
  frozen executable.
- Do not start workers from a module that imports PySide6 at import time.
  `freeze_support` runs first, but only if the worker re-enters
  `frozen_entry.py` rather than a Qt import at the top of `backtester`.
- Do not import the GUI from `backtester/__init__.py`.
- Child processes do not see `USERPROFILE` or `LOCALAPPDATA`.

**Desktop GUI**

- Put the window in the package `backtester.gui`.
- Add a `gui` subcommand so `backtester gui` opens it. The windowed
  executable prefers that command, and otherwise calls
  `backtester.gui.main`.
- Declare the libraries as the optional extra `gui` (PySide6 and
  pyqtgraph). The installer build installs that extra when the extra
  exists. The spec then collects PySide6, including the `platforms`,
  `styles`, and `imageformats` plugins, and pyqtgraph.
- Use PySide6 only. The build excludes PyQt5, PyQt6, and PySide2 so a
  single Qt binding is frozen.
- `--launcher-check` is reserved. It exits without opening a window.
- Read `data_root`, `runs_dir`, and `registry_path` from
  `%LOCALAPPDATA%\StrategyBuilder\config.json`. Do not hardcode the
  market-data path. `strategy_builder_config.ensure_user_config` is the
  reference implementation in this repo; inside the frozen bundle it is
  importable as `strategy_builder_config`.
- The windowed executable has no console: `sys.stdout` and `sys.stderr`
  may be `None`. The launcher replaces them with a null device before it
  calls `backtester gui`. GUI code should still tolerate a missing
  stream.
- Keep the executable names `backtester.exe` and `Strategy Builder.exe`.
  The launcher decides which program it is from the file name.
