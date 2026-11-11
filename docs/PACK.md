# Packs and the operator sidecar

`backtester.pack` inspects a registered series, optionally anonymizes it, and writes a chunked pack plus an operator sidecar. The sidecar is never placed inside the pack directory. Library code does not print the scale factors. The CLI says only that they live in the sidecar.

## Anonymized pack

Prices (open, high, low, close, wap) are multiplied by `price_factor` k. Volume is multiplied by `volume_factor`. Both factors are drawn from a log-uniform range away from 1 and away from 0.9 when the operator does not pass them. k of 1 is refused: an anonymized pack has to actually rescale.

Calendar dates become session ordinals starting at 1. The session id is `S00001`, `S00002`, ... The file names are `bars_part_NNN.csv.gz`, `daily_rth.csv.gz`, and `manifest.json`. The manifest calls the asset `ASSET`.

The pack does not contain the ticker, a real year in a file name, an ISO date, the date map, or any scale factor. Those are only in the sidecar: symbol, both factors, `share_scale` (equal to k), the ordinal-to-date map, borrow, the source path, and an operator note.

A named pack is the other switch. It keeps real dates and forces every factor, including `share_scale`, to 1. The CLI makes the choice explicit: `--anonymize` or `--named`.

## Chunks

The target is about 250 trading sessions per file, and every file stays under 25 MB. A single session that does not fit is an error. A session is never split across files. Gzip members are written with mtime 0 so two builds of the same rows hash the same.

Bar size is fixed when the pack is built. `run --pack` does not resample again. Build another pack to change the bar size.

## Per-share fees

Prices times k means `share_scale` = k. Pack share counts are about 1/k of the real count. Every anonymized run goes through `run_pack`. That function reads the sidecar in the operator process and passes k into the engine as `share_factor` / `share_scale`. It does not pass the sidecar document, the date map, the ticker, the volume factor, or the operator note.

Per-share fees (commission, the monthly tier count, exchange, clearing, CAT, TAF) use real share counts: `abs(pack shares) * k`. Value-based fees (SEC, and the commission and clearing caps) use pack shares times pack price, which is the real trade value. `fee_mult` is still the Desk stress switch and does not scale borrow, financing, or the tier count.

The true factor stays in the sidecar. `metrics.json` records the cost basis that was used: share scale, profile, schedule, and that per-share fees were real share counts. The report does not include the date map or the volume factor.

If the series borrow was recorded as the profile default, the run uses the borrow rate of the profile you select. One pack can be run on Arena and on Desk. An explicit borrow overrides both. A raw dividend file is refused on an anonymized pack, because dividend cash is shares times amount and the pack's share counts are rescaled.

## Residual leaks

These are the leaks the plan already names. Scrambling further does not remove them, and this builder does not pretend to.

- Weekday plus early-close and holiday gaps can be lined up with a real calendar. Weekday stays in the file because the engines and the strategies receive it. The ordinal calendar hides the year; it does not hide the shape of the week.
- A crash-shaped return path is recognizable without a date.
- Native tick times `price_factor` can reveal the scale, because the price grid is still a grid after a constant rescale.

Mitigation is the rule set (no identification, no external market data, no sidecar in the designer tree) plus an audit of the pack, not another hash of the prices. `assert_anonymized_pack_clean` is that audit for the fields that must be absent: ticker, years in file names, ISO dates, and the sidecar keys.
