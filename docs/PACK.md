# Packs and the operator sidecar

`backtester.pack` inspects a registered series, optionally anonymizes it, and writes a chunked pack plus an operator sidecar. The sidecar is never placed inside the pack directory. Library code does not print the scale factors. The CLI names the sidecar file and does not print the numbers.

## Anonymized pack

Prices (open, high, low, close, wap) are multiplied by `price_factor` k. Volume is multiplied by `volume_factor`. Both factors are drawn from a log-uniform range away from 1 and away from 0.9 when the operator does not pass them. k of 1 is refused: an anonymized pack has to actually rescale.

Calendar dates become session ordinals starting at 1. The session id is `S00001`, `S00002`, ... The file names are `bars_part_NNN.csv.gz`, `daily_rth.csv.gz`, and `manifest.json`. The manifest calls the asset `RANDOM`. A ticker that matches a reserved report token is refused before any pack file is written. The README lists those tokens. The command exits 2.

The pack files do not contain the ticker, a real year in a file name, an ISO date, the date map, or a scale factor. The sidecar is the file that stores them: symbol, both factors, `share_scale` (equal to k), the ordinal-to-date map, borrow, the source path, and an operator note. The worker that runs the engine receives `share_scale` for the invoice. A strategy file does not. It runs in the same jail as the referee, and that process is not given the scale, the sidecar, or the calendar. The worker is not a second copy of the sidecar.

A named pack is the other switch. It keeps real dates and forces every factor, including `share_scale`, to 1. The CLI makes the choice explicit: `--anonymize` or `--named`.

## Chunks

The target is about 250 trading sessions per file, and every file stays under 25 MB. A single session that does not fit is an error. A session is never split across files. Gzip members are written with mtime 0 so two builds of the same rows hash the same.

Bar size is fixed when the pack is built. `run --pack` does not resample again. Build another pack to change the bar size.

## Per-share fees

Prices times k means `share_scale` = k. Pack share counts are about 1/k of the real count. Every anonymized run goes through `run_pack`. That function reads the sidecar in the operator process and passes k into the engine as `share_factor` / `share_scale`. It does not pass the sidecar document, the date map, the ticker, the volume factor, or the operator note.

Per-share fees (commission, the monthly tier count, exchange, clearing, CAT, TAF) use real share counts: `abs(pack shares) * k`. Value-based fees (SEC, and the commission and clearing caps) use pack shares times pack price, which is the real trade value. `fee_mult` is still the Desk stress switch and does not scale borrow, financing, or the tier count.

`metrics.json` records the cost basis that was used: share scale, profile, schedule, and that per-share fees were real share counts. The report does not include the date map or the volume factor.

An anonymized run also writes `designer/`. That directory is the copy to hand a strategy designer. It has no ticker, no calendar date, and no scale-factor field. The operator `metrics.json` next to it still has `cost_basis.share_scale`. A parallel batch does the same split: operator files under `jobs/`, and one designer bundle under `designer/` with the leaderboard and each anonymized job. Hand over that `designer/` directory only.

If the series borrow was recorded as the profile default, the run uses the borrow rate of the profile you select. One pack can be run on Arena and on Desk. An explicit borrow overrides both.

Dividends are real dollars per real share, keyed by calendar date. On an anonymized pack the parent multiplies each amount by `share_scale` and remaps the date through the sidecar date map to the pack session id (`S00001`, ...). The child receives only that dict: no calendar key and no unscaled amount. A date that is not in the pack is an error. Dividends are not written into the pack.

## Residual leaks

These are the leaks the plan already names. Scrambling further does not remove them, and this builder does not pretend to.

- Weekday plus early-close and holiday gaps can be lined up with a real calendar. Weekday stays in the file because the engines and the strategies receive it. The ordinal calendar hides the year. The shape of the week stays visible.
- A crash-shaped return path is recognizable without a date.
- Native tick times `price_factor` can reveal the scale, because the price grid is still a grid after a constant rescale.
- The strategy process does not receive `share_scale`. The worker runs the engine and bills there. Post-fee cash is still in the account sent one bar at a time, so the public fee schedule can be lined up with that cash. The factor itself is not on a frame the strategy can see.
- Bar `count` is the source trade count. It is not multiplied by `volume_factor`, so an unchanged count is an identity cue. The builder leaves `count` alone.

Mitigation is the rule set (no identification, no external market data, no sidecar in the designer tree) plus an audit of the pack, not another hash of the prices. `assert_anonymized_pack_clean` is that audit for the fields that must be absent: ticker, years in file names, ISO dates, and the sidecar keys.
