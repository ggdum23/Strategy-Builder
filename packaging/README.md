# Packaging

The Windows installer lives here. Build and install steps are in
`docs/INSTALLER.md`.

Library code stays importable without printing, without reading the working
directory, and without loading market data. The GUI calls those modules.
Printing stays in `backtester.__main__`.

- Console entry for PyInstaller: `packaging/frozen_entry.py`, which calls
  `backtester.__main__:main` after `multiprocessing.freeze_support()`.
  The installed program is `backtester.exe`.
- Windowed entry, same file: `Strategy Builder.exe` runs `backtester gui`
  when the `backtester.gui` package exists, and a short placeholder until then.
- `backtester.desktop:main` is the older empty seam. The launcher does not
  call it.
- The bundle carries its own Python. The machine interpreter is not required.
- Inno Setup runs the bundled executables. It does not copy or unzip market
  data. `--src` points at files that are already on disk.
- `run.ps1` and `run.bat` remain the source-checkout helpers. They are not
  what the installed program runs.
