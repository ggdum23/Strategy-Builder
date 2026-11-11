# Series registry

A ticker is a registry entry plus an ingest. The loader does not grow a branch per symbol. `backtester.registry` is the importable API (dataclasses and dicts, no printing). The CLI is a thin caller.

The file is JSON, `schema_version` 1. JSON is the registry format because the reader and writer are the standard library: no extra install, and the same file opens on the Windows machine and in CI. A reader rejects a version, a key, or a type it does not know. It does not guess.

## What one series records

| Field | Meaning |
| --- | --- |
| `symbol`, `source_root`, `source_glob` | Where the monthly filled files live. `source_root` is whatever path was ingested. It is not a path baked into the program. |
| `columns` | The filled-file columns that were present. |
| `timezone` | Offsets actually observed on `timestamp_et`. A daylight-time pair is the two US Eastern offsets one hour apart. Any other offset contradicts the column name and the ingest fails. |
| `session_segments` | Clock window inferred for each label: modal first minute, last minute, and bar count, plus variants, gaps, and the engine phase. `hours_assumed` is always false. |
| `price_adjustment` | `raw` or `total_return`. Total-return is recorded and is not runnable in this phase. Prices are not rewritten. |
| `splits` | Declared `YYYY-MM-DD` and ratio. Recorded, not applied. A non-empty list is not runnable. |
| `fill_policy` | `flag_present_none_set` when every `filled` flag is 0, or `stale_forward_fill` when `filled=1` bars are flat, zero-volume, and equal to the previous close. Anything else fails. |
| `native_tick` | Greatest common grid of the trade prices (open, high, low, close), not of WAP. Fewer than two distinct prices fails. |
| `native_bar_minutes` | 1. These files are one-minute bars. |
| `typical_volume` | Median bar volume and median session volume. |
| `share_factor` | 1.0. Raw files are not rescaled. An anonymized factor lives only in the operator sidecar. |
| `borrow_rate_annual`, `borrow_rate_source` | `profile_default` stores that profile's rate and, at pack time, does not freeze it. A run then uses the profile you actually select. `explicit` is the rate you passed and it overrides both profiles. |
| `profile`, `cost_schedule` | `arena` or `desk`. The two schedules are not merged. The run command still chooses the engine. |
| `phase_map`, `original_session_labels` | When the file does not already say `pre` / `rth` / `post`, the map is the operator's record of which label is which phase. Unknown labels without a map fail. |
| `runnable` | False for total-return or a declared split list. |
| `n_rows`, `n_sessions`, `first_timestamp`, `last_timestamp` | The native one-minute series, not a resampled delivery. |

Session hours are the modal window in the file. A 08:00–14:00 tape is stored as 08:00–14:00. Nothing fills in 04:00–19:59. When two shapes are equally common, the listed window is the longest one (then the later close, then the earlier open) and the inspection says the modal was not unique. Every other shape is a variant. The engine's `short_session` flag is a separate rule: the regular segment's last one-minute bar is before 15:59. When that flag and the inferred window disagree, the inspection notes say so.

## Commands

```text
python -m backtester ingest --ticker SYM --src DIR --registry registry.json --profile arena|desk --price-kind raw|total_return
python -m backtester registry list --registry registry.json
python -m backtester registry show --registry registry.json --ticker SYM
```

`--out` still writes the inspection JSON and is optional when `--registry` is set. Recording a series requires `--profile` and `--price-kind`. Omitting either is an error, not a default guessed from the ticker.

`--borrow-rate` records an explicit rate. Omit it to record the chosen profile's rate.

`--splits 2020-08-31:4` or `--splits none`. Declared splits are stored and not applied.

`--phase-map morning=pre,core=rth` renames labels. Two labels may not share a phase.
