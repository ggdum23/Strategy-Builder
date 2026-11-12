# Designer figures in the desktop window

The desktop window is specified with the GUI. This page is the cost-model rule that window has to follow when it draws a designer book.

A designer book is the tree under `designer/`. Its fills were priced with the flat designer cost model (`docs/COSTS_DESIGNER.md`), and its equity is the canonical $1,000,000 index. The operator book beside it is the real fee schedule and the real account.

Cost drag in basis points, on a designer book, is that book's `fees_total / initial_equity * 10000`, rounded half-up to 0.1 bp. `initial_equity` is the canonical index. The drag is the flat model's drag. It is not computed from the IBKR per-share schedule, and it is not a path back to a share count or a price.

The window should show the label `designer cost model: 0.8 bp flat` with those figures. The operator report keeps the real-fee numbers and can show `cost_comparison.json` beside them. That comparison file is not part of the designer tree.
