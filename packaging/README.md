# Packaging seam

Phase 1 does not build an installer. The layout is meant to take one later.

- Library entry: import `backtester.runner`, `backtester.data`, `backtester.registry`,
  `backtester.pack`, `backtester.engine_arena`, `backtester.engine_desk`. Importing
  them does not read the working directory, does not print, and does not load
  market data. The GUI should call those modules. Printing stays in `backtester.__main__`.
- CLI entry for PyInstaller: `backtester.__main__:main`.
- Window entry, still empty: `backtester.desktop:main`.
- A Windows build should bundle its own Python. The operator machine's system
  Python is 3.14 and is not a dependency of the installed app.
- Inno Setup is a later step. It should run the bundled executable. It should
  not copy or unzip market data onto the Desktop; `--src` points at the tree
  that is already there.
- Console scripts: `backtester` from the installed package, and `run.ps1` / `run.bat`
  from a source checkout.
