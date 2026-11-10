# Sample month

One month of raw 1-minute bars, October 2026, for QQQ and SPY.

`QQQ/filled/QQQ_1min_filled_2026-10.csv.gz`
`SPY/filled/SPY_1min_filled_2026-10.csv.gz`

Columns: `timestamp_et,epoch_utc,open,high,low,close,volume,wap,count,filled,session`.
`session` is `pre`, `rth`, or `post`. Prices are the raw prints in the file.
`share_factor` for this loader is 1.0. These files are not the full history.
