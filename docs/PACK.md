# Packs and the operator sidecar

`backtester.pack` inspects a registered series, optionally anonymizes it, and writes a chunked pack plus an operator sidecar. The sidecar is never placed inside the pack directory. Library code does not print the scale factors. The CLI names the sidecar file and does not print the numbers.

## Anonymized pack

Prices (open, high, low, close, wap) are multiplied by `price_factor` k. Volume is multiplied by `volume_factor`. Both factors are drawn from a log-uniform range away from 1 and away from 0.9 when the operator does not pass them. k of 1 is refused: an anonymized pack has to actually rescale.

Calendar dates become session ordinals starting at 1. The session id is `S00001`, `S00002`, ... The file names are `bars_part_NNN.csv.gz`, `daily_rth.csv.gz`, and `manifest.json`. The manifest calls the asset `RANDOM`. A ticker that matches a reserved report token is refused before any pack file is written. The README lists those tokens. The command exits 2.

The pack files do not contain the ticker, a real year in a file name, an ISO date, the date map, or a scale factor. The sidecar is the file that stores them: symbol, both factors, `share_scale` (the builder writes it equal to k), the ordinal-to-date map, borrow, the source path, and an operator note. The operator child receives `share_scale` so the real invoice can use real share counts. The designer child does not. That split is described below. The sidecar is not copied into either child.

A named pack is the other switch. It keeps real dates and forces every factor, including `share_scale`, to 1. The CLI makes the choice explicit: `--anonymize` or `--named`.

## Chunks

The target is about 250 trading sessions per file, and every file stays under 25 MB. A single session that does not fit is an error. A session is never split across files. Gzip members are written with mtime 0 so two builds of the same rows hash the same.

Bar size is fixed when the pack is built. `run --pack` does not resample again. Build another pack to change the bar size.

## Two cost models

Prices times k means the builder writes `share_scale` = k. Pack share counts are about 1/k of the real count. Every anonymized run goes through `run_pack` or `walk_pack`. The parent reads the sidecar, then drops the document before either child starts. Neither child receives the sidecar, the date map, the ticker, the volume factor, or the operator note.

The operator child is the real invoice. Per-share fees (commission, the monthly tier count, exchange, clearing, CAT, TAF) use real share counts: `abs(pack shares) * share_scale`. Value-based fees (SEC, and the commission and clearing caps) use pack shares times pack price, which is the real trade value. `fee_mult` is still the Desk stress switch and does not scale borrow, financing, or the tier count. The loader accepts a billing scale other than k, including 1, so that invoice can be stated on its own. The builder still writes `share_scale` equal to k.

The designer child is a second process. It uses the flat fee in `docs/COSTS_DESIGNER.md` and the canonical $1,000,000 index. It is not given `share_scale`, `share_factor`, or the operator account size. There is no per-share commission and no per-share SEC or TAF on that run. The same flat constant applies to every ticker.

`metrics.json` next to the report is the operator book. It records the cost basis that was used: share scale, profile, schedule, and that per-share fees were real share counts. The report does not include the date map or the volume factor. `cost_comparison.json` is also an operator file. It places the real-fee result beside the designer-cost result and the difference (designer minus real).

An anonymized run also writes `designer/`. That directory is the copy to hand a strategy designer. It has no ticker, no calendar date, and no scale-factor field. Equity in that tree starts at the canonical index, so the dollars do not follow the real account. The label on those metrics is `designer cost model: 0.8 bp flat`. A parallel batch does the same split: operator files under `jobs/`, and one designer bundle under `designer/` with the leaderboard and each anonymized job. The designer leaderboard is ranked on the designer-cost excess, not the operator excess. Hand over that `designer/` directory only.

If the series borrow was recorded as the profile default, the run uses the borrow rate of the profile you select. One pack can be run on Arena and on Desk. An explicit borrow overrides both.

Dividends are real dollars per real share, keyed by calendar date. The operator child receives each amount multiplied by `share_scale`, remapped through the sidecar date map to the pack session id (`S00001`, ...). The designer child receives the file amount remapped to the same session id and not multiplied by the scale. A date that is not in the pack is an error. Dividends are not written into the pack. Neither child receives a calendar key.

## Residual leaks

These are the leaks the plan already names. Scrambling further does not remove them, and this builder does not pretend to.

- Weekday plus early-close and holiday gaps can be lined up with a real calendar. Weekday stays in the file because the engines and the strategies receive it. The ordinal calendar hides the year. The shape of the week stays visible.
- A crash-shaped return path is recognizable without a date.
- Native tick times `price_factor` can reveal the scale, because the price grid is still a grid after a constant rescale.
- The exact-equity plus per-share-schedule residual is closed on designer surfaces. A designer run has no per-share term, is not given `share_scale`, charges one fixed basis-point constant, and reports equity on the canonical index. Exact equity in that tree plus the public per-share schedule does not recover a share count or a price, because that schedule is not what was charged and the equity is not the real account. The operator child still receives `share_scale` and still bills the real schedule. Post-fee cash on the operator book can still be read against that schedule. That book is not handed to a designer.
- Bar `count` is the source trade count. It is not multiplied by `volume_factor`, so an unchanged count is an identity cue. The builder leaves `count` alone.

Mitigation is the rule set (no identification, no external market data, no sidecar in the designer tree) plus an audit of the pack, not another hash of the prices. `assert_anonymized_pack_clean` is that audit for the fields that must be absent: ticker, years in file names, ISO dates, and the sidecar keys.
