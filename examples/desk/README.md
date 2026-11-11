# Desk examples

`buy_and_hold.py` returns 1.0. `two_x.py` returns 2.0. Both go through
`engine_desk.py` with that run's fees, borrow, financing, and liquidation.
The 2x book is financed at 7% unless `--financing-rate` overrides it.
