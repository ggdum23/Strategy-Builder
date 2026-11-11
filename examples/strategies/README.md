# Designer strategies

`D01` through `D20` are the original arena designer strategies. Each directory has
`strategy.py` and `PROFILE.md`. They speak the Arena bar (`day`, `minute`, `session`,
`state["target"]`, optional `on_day_end`). None of them set `WANTS_EXTENDED_HOURS`,
and none of them go short. Several are levered inside the Arena cap of 2
(`D02`, `D14`, and `D20` reach 2x).

The parity test runs all twenty on the October 2026 QQQ sample, plus a subclass of
`D02` that only flips extended hours on, plus a one-line short. Those two extras
exist because the originals do not cover those paths.

Desk examples live in `examples/desk/`. A designer file that reads `state["target"]`
is an Arena strategy; the Desk account dict does not have that key.
