# Desk spec decisions

`sim.py` is not in the reference archive. `src/backtester/engine_desk.py` is the
implementation of PLAN.md Profile B. It does not call the Arena engine, Arena
`fill_costs`, or Arena `slipped_price`. The numeric schedule is duplicated in
`costs_desk.py`.

Arena FINRA TAF stays on the vendored reference schedule: `$0.000195` per share
sold, cap `$9.79`, and SEC `$0.0000206` of sale value (`src/backtester/costs.py`,
byte-identical to `tests/reference/arena/costs.py`). PLAN.md says Arena TAF is
`$0`. Parity with the vendored engine wins. Those IBKR constants are not edited
to match that sentence. See `docs/ARENA_PORT_NOTES.md` and item 9.

These are the readings that were not fixed by a single sentence in the spec.

1. **Separate engine.** Desk fill venue, leverage cap, financing, borrow, dividends,
   fee table, warm-up, and liquidation are not switches inside the Arena loop.

2. **Bar keys.** The strategy sees exactly `session`, `time`, `phase`, `open`,
   `high`, `low`, `close`, `volume`, `wap`, `count`, `filled`, `new_session`.
   `minute` stays internal. `new_session` is recomputed from the session id, so a
   flag on the input cannot move a financing or dividend charge. Named local
   data uses the calendar date (`YYYY-MM-DD`) as the session id. A caller may
   pass `S00001` instead. Arena's integer `day` is not shown to a Desk strategy.

3. **Next RTH open.** A queued order fills on the next bar whose phase is `rth`,
   including the next minute of the same session. It does not wait for the next
   date. Only the token `rth` counts. `filled == 1` does not block a fill and
   does not rewrite the open. A synthesized bar is a fill bar at the open the
   file already carries (the spec's "open is the previous close" is a property
   of that bar, not a second price the engine invents).

4. **Hold.** `None` keeps the current target and does not cancel an unfilled
   order. The stored target starts at 0. Returning the same number after clipping
   does not send another order, so a held target does not rebalance for drift.
   Returning `0.0` while the target is already 0 sends nothing.

5. **Clip, don't reject.** A finite target outside `[-3, 3]` is clipped. `bool`
   and `str` raise. Arena still `float()`-coerces those; Desk does not. Integers
   and numpy real numbers are accepted. A non-finite target raises.

6. **Share count.** `int(target * equity_at_fill_open / fill_open)` truncates
   toward zero. Equity is cash plus shares times the unslipped open, after the
   session-boundary charges and before slippage. If that equity is not positive,
   the order is sized flat (0 shares). Whole shares only.

7. **Slippage.** 1 bp adverse, buys up and sells down. `--fee-mult` multiplies
   the bps. Liquidation uses that bar's close as the reference price, then the
   same 1 bp and the same fee function.

8. **Pass-through.** `commission * (0.000175 + 0.00056)`, the Arena reference
   formula, copied into the Desk file. Not a flat `$0.000175`, and not
   `$0.000175` per share plus `0.00056 * commission`. The PLAN sentence is
   ambiguous; the reference implementation is the schedule that must not be
   simplified. The two modules do not import each other.

9. **Desk sell fees.** SEC is `27.80e-6` times sale value (`abs(shares) * slipped
   price`, not scaled by `share_factor`). FINRA TAF is `min(0.000166 * abs(shares)
   * share_factor, 8.30)` on sells only. There is no TAF holiday calendar. Arena's
   TAF and SEC stay on the vendored reference numbers, not on PLAN.md: TAF is
   `$0.000195` per share sold with a `$9.79` cap, and SEC is `$0.0000206` of
   sale value. PLAN.md's Arena "TAF $0" is not applied. Parity with the vendored
   engine wins, and those IBKR constants do not change. See `docs/ARENA_PORT_NOTES.md`.

10. **The rest of the IBKR invoice.** Tier walk, `$0.35` minimum, 1% cap,
    exchange `$0.003`, clearing `$0.0002` capped at 0.5% of value, and CAT
    `$0.000003` match the Arena order of operations, including minimum-then-cap
    (the cap can pull commission under `$0.35`) and capped orders adding 0 shares
    to the monthly tier. Per-share pieces use `abs(shares) * share_factor`.
    Value for the caps and for SEC does not.

11. **`--fee-mult`.** The schedule is computed first, including the minimum and
    the cap. The multiplier is then applied to each dollar component, and to
    slippage bps. A stress invoice can exceed 1% of value. The multiplier does
    not scale borrow, financing, dividends, or the share count that advances the
    tier. Financing dollars can still change because cash changes.

12. **`share_factor`.** Raw runs are 1.0. 0.9 is not a kit constant. Passing
    any other factor on a raw run is an error. An anonymized pack keeps its
    scale in the operator sidecar, and the operator child receives that scale
    so per-share fees use real share counts. The designer child does not. It
    uses the flat fee in `docs/COSTS_DESIGNER.md`.

13. **Tier month.** `live_session_ordinal // 21`. Warm-up does not advance it:
    going live clears the volume bucket, and the ordinal is counted from the
    first live session. This is not a calendar month and it is not reset per symbol
    inside one run (a multi-name book is a later phase).

14. **Financing.** One charge per session boundary, at the new session's first
    bar, before the fill. The position is valued at the previous session's last
    bar close. That is the last bar in the session, including post when the grid
    has post. The daily equity mark is a different price: the last RTH close.
    Amount is `rate/252 * max(0, |shares * price| - equity)` with rate 0.07 unless
    overridden. The formula is the whole rule. There is no extra "only if leverage
    is above 1" gate; a 1x book can accrue a few dollars when fees and slippage
    make `|position value|` exceed equity. The sentence about levered longs and
    shorts beyond 1x describes when the charge is large. Idle cash earns 0. No
    charge before the first live session (there is no previous live close, and
    warm-up is flat). No charge after the last bar. A weekend is one boundary,
    not three calendar days: session ids are not parsed into a day count.
    After disqualification, charges stop.

15. **Borrow.** `0.0025/252` of short notional, same boundary and same mark as
    financing, not at the RTH close (that is the Arena convention). Longs pay
    zero. `--borrow-rate` overrides. It is not a ticker table.

16. **Dividends.** `shares_held_into_the_ex_session * amount`, using the shares
    from the previous session's snap, credited or debited on cash before that
    session's fill. A positive amount credits longs and debits shorts. A missing
    session is zero. No file means no dividends. Keys in the operator file are
    calendar dates (`YYYY-MM-DD` for named files, which are also the session
    ids the named engine is given). An anonymized pack run takes those same
    real dollars per real share. The parent remaps the date through the sidecar
    date map to the pack session id (`S#####`). The operator child receives the
    amount multiplied by `share_scale`. The designer child receives the amount
    multiplied by the price factor whose operator HMAC matches, not by
    `share_scale`. That MAC (version 2) also binds the pack id, the anonymized
    flag, the date map, and the borrow rate (null stays null). Session ids use
    ASCII digits. A date that is not in the pack is an error. The operator run
    manifest records a sha256 of the dividend file when one is passed. That
    hash is not written under `designer/`. On the designer cost-model path the
    engine drops `share_factor` before `on_bar`.

17. **Bar order.** If this is the first live bar, reset the book flat to the
    starting equity. Then, if not disqualified: boundary charges, then an RTH
    fill of any pending order, then mark and high-water update, then the
    liquidation test, then the strategy, then the margin check. Worst equity/HWM
    is sampled at the end of the bar. The daily mark is written on the last RTH
    bar. The financing snap is written on the last bar of the session, after
    any fill on that bar. A post bar does not fill. A target set there waits
    for the next RTH open, which is after the boundary charge, so the snap
    still has the shares held through the close.

18. **Margin call.** On every RTH bar close, after the strategy has spoken, if
    there is no pending order, the position is not flat, equity is positive, and
    gross leverage is strictly above 3.15, queue a fill to `sign(position) * 3`
    at the next RTH open. Equality at 3.15 does not fire. The check is not
    limited to the last RTH bar of the session: the spec says "an RTH close",
    and it uses "last RTH close" only for the daily mark. Pre and post closes
    do not queue a margin call. The resize is sent even when the stored target
    is already ±3, because the share count is stale. A later numeric signal
    that differs from ±3 replaces the unfilled margin order; `None` does not
    cancel it. The forced target is stored, so a later identical return does
    not queue a second resize on top of the one already pending. If equity is
    not positive, the liquidation test has already run.

19. **Liquidation.** After the high-water mark is updated to include this bar's
    pre-trade equity, equity strictly below `0.30 * HWM` flattens at this bar's
    close with Desk fees and 1 bp. Equality does not liquidate. The mark is the
    running peak, including the starting equity, not the starting equity alone.
    The run does not raise. The account is flagged `disqualified`. The curve
    continues, flat, so the sample is not shortened. The strategy is still
    called on later bars with `liquidated=True`, and the return is ignored.
    No further financing, borrow, or dividends. Gross leverage for the max/avg
    stats is sampled before the flatten, including the trigger bar. Worst
    equity/HWM uses end-of-bar equity, including the flat tail.

20. **Where a post liquidation shows up.** Daily equity is the last RTH close
    after that bar's processing, so an RTH-close liquidation is in that
    session's daily point. A post-only liquidation is in worst equity/HWM
    immediately and in the daily series on the next RTH close.

21. **Warm-up.** `live_start_ordinal` counts sessions from the start of the
    frame. While `live` is false the strategy is called, its return is ignored,
    and nothing is stored as a pending order. The book stays at the starting
    equity with zero shares. On the first live session the book is reset flat
    to the starting equity, the high-water mark and the fee-volume bucket reset,
    and there is no financing for the warm-up overnight. This is not Arena's
    rescale-and-keep-exposure. A target returned on a warm-up bar does not fill
    on the first live open.

22. **Benchmarks.** 1x and 2x go through this engine with the same fees, borrow,
    financing, fee multiplier, share factor, dividends, and warm-up. The 2x
    book is financed at the run's financing rate (7% by default). Arena's
    benchmark is only 1x, and it is RTH-only even if the strategy wants extended
    hours, matching the reference runner.

23. **Floats.** IEEE float, not `Decimal`. The SPY designer-pack engine is a
    different program and is not this profile.

24. **Non-positive equity and gross leverage.** Gross leverage is infinite when
    equity is not positive and the share count is not zero. Liquidation (30% of
    a positive high-water mark) runs first on a negative mark, so the margin
    check does not see that bar.

25. **Splits.** A non-empty split list with raw prices raises. Nothing in the
    loader applies a hidden QQQ or SPY factor. A total-return switch is later work.

26. **`short_session`.** Measured on the 1-minute grid before resampling: 1 when
    the last RTH minute of that date starts before 15:59. A resampled bucket
    does not recompute it. Arena strategies see the flag. Desk's strategy view
    does not include it.

27. **Resample.** Group by `(day, phase)`. A minute that does not increase
    starts a new segment. The anchor is the segment's first minute, not
    midnight. A partial tail is kept. `filled` on the bucket is 1 only when
    every source minute is filled. WAP is volume-weighted, and the close when
    volume is 0. `N=1` is a copy. A bar never contains two phases or two sessions.
    A gap inside a segment still labels the bucket at the anchored start.

28. **Configuration versus constants.** Fee month length, share factor, borrow,
    financing rate, data root, dividends, and splits are run configuration.
    The tier breakpoints and the two official sell-fee schedules are constants.
    They are not copied from a ticker.

29. **Margin target sticks.** After a margin call, the stored target becomes ±3
    so a later identical return does not stack another order on the pending one.
    The fill still happens once.

30. **`on_day_end`.** Desk does not call it. Arena still does.

31. **Disqualification does not raise.** The run finishes and writes metrics,
    including the post-mortem (`session`, `time`, `phase`, `close`, equity and
    shares before and after, and the high-water mark).

32. **Leverage stats.** Max and average gross leverage use live bars before
    disqualification, including flat bars and the trigger bar's pre-trade gross.
    Worst equity/HWM uses every live bar's end-of-bar equity, including the flat
    tail after a knockout.

33. **Walk-forward warm-up is per profile.** The default warm-up length is 0.
    There is no default design cut. Arena locked warm-up uses `reset_day`.
    Desk locked warm-up uses `live_start_ordinal`. They are not the same code path.
    The design book and the locked book each run in a new interpreter started
    with the spawn method. The design interpreter receives only the design frame.
    The locked interpreter receives only the locked window plus its warm-up, and
    it does not see objects created by the design run. While the design child
    runs, the parent does not keep the locked window, and the on-disk spill is
    sealed so it is not an openable pickle. Phase 1 isolates the heap,
    class attributes, module globals, environment, argv, and cwd. It does not
    confine the filesystem or another process's memory. See docs/ISOLATION.md.

34. **Performance math.** 252 sessions per year, sample standard deviation with
    delta degrees of freedom 1, Sortino uses the square root of the mean of
    squared negative returns, risk-free rate 0. Same definitions as the Arena
    `metrics` function, applied to Desk's last-RTH-close marks. Non-finite
    numbers (a Calmar ratio with no drawdown) are written as JSON null.
    Every field, the 1x and 2x benchmarks, and the leaderboard key are defined
    in docs/METRICS.md.

35. **Account when equity is not positive.** `exposure` and `gross_leverage` are
    null in the dict handed to the strategy. `position_shares` is the position.
    On a liquidation bar the strategy is called after the flatten, with
    `liquidated` true.

36. **First live session and dividends.** There is no previous live close, so
    the first live session does not pay a dividend even if that date is listed.
    A flat book would have made the cash zero anyway.

37. **Cash interest is off unless asked.** Idle cash is
    `max(0, equity - |shares * price|)` on the pre-charge snap. One accrual is
    `cash_rate / sessions_per_year * idle`, default sessions 252. The default
    cash rate is 0, and that 0 does not add to cash. Financing stays the
    existing `financing_due` call, including a zero charge when nothing is
    borrowed, so a default book matches a book that passes 0.07, cash 0, and
    withholding 0. An omitted desk rate is `FINANCING_RATE_ANNUAL` read when
    `execute` starts, which is 0.07 unless that constant was patched in the
    process. The parent recomputes benchmarks against the unpatched constant.

38. **Withholding.** The default rate is 0, and the dividend line stays
    `shares * amount`. A positive rate pays a long
    `shares * amount * (1 - rate)` when the amount is positive, and a short
    still pays the gross amount. Above 1.0 or below 0 is an error.

39. **Price basis is not a second engine.** Desk fill, leverage, borrow,
    liquidation, and the 7 percent financing default stay in this module.
    `price_basis=total_return` either runs a registry total-return series with
    no dividend file, or credits `dividends.csv` on raw prices. The two
    profiles are not merged.
