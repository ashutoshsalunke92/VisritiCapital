# Tuning Guide — what changed, why, and how to use it

Your original code was already sound engineering — clean separation between
broker plumbing and strategy logic, a real SL/TP state machine, honest
caveats in your own comments about ATM-strike accuracy. Nothing here rips
that up. Same 4-leg iron fly (sell ATM straddle, buy OTM7 wings), same
entry/exit state machine shape. What changed:

## 1. Hedge legs no longer get stopped out on noise (the main fix)

**File: `strategy.py`, `config.py`, `backtest_engine.py`**

Your original `per_leg_sl_pct` (25%) applied identically to all 4 legs —
both the two short ATM legs *and* the two long OTM7 hedge legs. The problem:
hedge legs are cheap, deep-OTM options. A ₹6 premium losing 25% is ₹1.50 —
inside normal bid-ask noise, not a real move. When that noise trips the old
per-leg SL, your code closed the hedge and left the corresponding short leg
running unprotected — the opposite of what a hedge is for.

New default: hedge (BUY) legs are **never** closed on a per-leg basis. They
ride until the combined strategy SL/TP or the time exit closes everything
together. You can restore the exact old behavior by setting
`HEDGE_LEG_SL_PCT=0.25` (or whatever `PER_LEG_SL_PCT` is) in `.env` — worth
doing once, just to A/B the two against your own 6 months of data and see
the difference for yourself rather than taking my word for it.

## 2. Optional entry-quality filter (off by default)

**File: `config.py`, `run_backtest.py`, `run_swing_backtest.py`**

`MIN_CREDIT_TO_WIDTH_PCT` in `.env`, default `0.0` (disabled — identical to
your original behavior, trades every day). If set to e.g. `0.20`, any day
where the net credit collected is less than 20% of the OTM7 wing width gets
skipped and logged separately as `SKIPPED_LOW_CREDIT`, instead of being
traded. The idea: on a low-IV day, the premium you collect may not
compensate for the wing risk you're carrying — skipping those days trades
frequency for quality. **Start with this at 0** to get your true baseline
first, then test raising it in the sweep tool below and compare.

## 3. A local data cache (`data_cache.py`)

Your existing scripts hit the Upstox API fresh every single backtest run —
fine for one run, painful for experimentation, especially since your token
expires daily. This cache stores each day's/week's fetched 4-leg minute
data locally after the first fetch. SL/TP/entry-time changes don't need new
API calls at all once a date range is cached — only changing
`OTM_WING_STRIKES` or `STRIKE_STEP` invalidates the cache (different
contracts entirely, which the cache key accounts for automatically).

## 4. A parameter sweep tool (`param_sweep.py`)

This is the actual answer to "what SL/TP/entry-time should I use" — instead
of me inventing numbers with no data, this runs a grid search of
entry-time × SL% × TP% × hedge-SL-mode against your real cached history,
and reports results **split into a train period and a held-out test
period** so you're not just curve-fitting the same 6 months you already
have.

```powershell
python param_sweep.py --from 2025-06-01 --to 2026-07-15 --mode intraday
```

Read the printed TRAIN vs TEST columns together. A combo that looks
amazing on TRAIN but falls apart on TEST was fitted to noise — skip it.
A combo where TRAIN and TEST both look reasonable and roughly consistent
is the one worth actually trading. Edit the grid ranges near the top of
`param_sweep.py` (`entry_times`, `per_leg_sl_pcts`, etc.) to search wider
or narrower once you've seen the first pass.

## Honest framing on "25%+ guaranteed, minimal drawdown"

I'm not a financial advisor and this isn't financial advice — but
mechanically, for a defined-risk premium-selling structure like this one,
return and drawdown are not independent knobs:

- **Sizing (more lots) scales both proportionally.** It's leverage, not edge.
- **A true minimum guaranteed profit isn't achievable** with short options
  exposed to overnight gap risk — a large gap can blow through both a
  per-leg SL and the combined SL before your logic gets a tick to react,
  no matter how the exit rules are tuned.
- Some of your current 6% may itself be a touch soft, per your own
  `data_fetcher.py` caveat: ATM strike selection is only pinpoint-accurate
  for the most recent ~30 days; older days fall back to prior-day close
  and can misplace ATM by a full strike step on gap days. Worth knowing
  before you tune hard against that 6% as ground truth.

What the four changes above give you is a genuine, defensible way to
*improve risk-adjusted return within the same structure* — not a promise
of a specific number. Run the sweep on your real data and see where it
actually lands; that number is more trustworthy than any number I could
give you without it.

## Suggested order of operations

1. Re-run `run_backtest.py` unchanged (hedge fix applied, filter off) to
   get your new baseline — compare directly against your old 6%.
2. Run `param_sweep.py --mode intraday` over the same window. Look at
   TRAIN vs TEST together.
3. Pick a consistent (not just highest-TRAIN) combo, put those values in
   `.env`, run `run_backtest.py` one more time as a final check.
4. Only then repeat for swing mode if you want to compare intraday vs
   positional on equal footing.
