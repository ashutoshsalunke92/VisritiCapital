# v2 Fix Log — what was broken, what changed, how it was verified

## 1. Delta Strangle "not working" — four real bugs, all fixed

- **Wrong-chain hedge rolls.** `adjustment_engine.execute_adjustment()` only
  received the monthly (`short_chain`); every roll priced its new hedge leg
  off the wrong expiry. Now takes both `short_chain` and `hedge_chain`, and
  `strike_selector.HedgedDeltaStrangleSelector` exposes per-side methods
  (`select_single_short` / `select_single_hedge`) so entry and every roll
  use the identical, correct chain per leg.
- **Stale strike monitoring.** The adjustment engine now reads
  `StrangleState.current_strikes()` every tick (new method) instead of a
  fixed entry-time strike that never updated after a roll.
- **Near-zero-DTE hedge chain.** `strangle_data_fetcher._resolve_expiries()`
  now requires two future weekly expiries and skips to the second one, like
  `data_fetcher.resolve_expiries()` already did for the Iron Fly.
- **Runaway API calls.** Chain building was pulling the entire 100+ strike
  contract list, per side, every 5 simulated minutes. Now bounded to
  `STRANGLE_STRIKE_WINDOW` (default 20) strikes around ATM, and each
  instrument's full day of candles is fetched **once** and cached
  (`.cache/`), not re-fetched every tick.

Verified with synthetic Black-Scholes-priced chains (see test output above)
that a roll now correctly prices the short off the monthly chain and the
hedge off the weekly chain, and that live strike tracking updates instantly.

## 2. Trailing SL

New `SL_MODE=TRAILING` (default stays `STATIC`, zero behavior change unless
you opt in). Once profit reaches `TRAIL_ACTIVATE_PCT` of the static TP, the
fixed TP is replaced by a stop that ratchets up behind the running peak and
exits on a `TRAIL_GIVEBACK_PCT` pullback from that peak. The static SL is
always still live underneath as a hard floor. Implemented identically in
`strategy.IronFlyState` and `strangle_strategy.StrangleState`.

## 3-5. No more per-run prompts

- Backtest/forward-test always use a fixed `BACKTEST_WINDOW_DAYS` (default
  **180**, per your instruction) lookback — no date prompt.
- Output is always `output/` — no path prompt.
- Capital is always ₹250,000 per runner (`FIXED_CAPITAL`) — no prompt.
All three are still changeable in `.env`, just not asked every session.

## 6. Monthly Iron Condor was aliased onto the weekly path

`trading_engine._run_iron_fly_session` now has a real monthly branch:
non-overlapping cycles, exit = that cycle's real monthly expiry minus
`IRON_CONDOR_MONTHLY_EXIT_DAYS_BEFORE_EXPIRY` (default 14) days, and its own
SL/TP thresholds (`resolved_monthly_sl_pct/tp_pct`, default 2x weekly's).
`data_fetcher.py` / `strategy.py` / `backtest_engine.py` — the parts you
said were already working — are untouched; only which dates and which
`StrategyParams` instance get passed in differs. Verified: weekly cycles
average ~5 day holds, monthly cycles now average 8-13+ days and land on
different exit dates entirely.

## 7. Backtest accuracy / real-data verification

- `strangle_data_fetcher._fetch_day_series()` logs every real Upstox call
  (`[DATA] Upstox real candles: <instrument_key> <date> rows=<n>`) so it's
  auditable that fills come from genuine expired-instrument candles, never
  fabricated.
- `verify_real_data()` sanity-checks every fetched series before it's
  trusted (non-empty, real timestamp column, at least one positive close).
- The near-zero-DTE and strike-window fixes above also directly improve
  accuracy (fewer degenerate IV solves, less garbage-in from illiquid
  strikes never actually near the target delta).

## 8. Forward-test now runs on real live data, not a relabeled replay

- Iron Fly: new `live_data_source.py` resolves today's real tradable ATM/OTM7
  legs via Upstox's live (non-expired) option-contract + LTP endpoints, and
  `trading_engine._run_iron_fly_live` polls real LTPs until exit time.
- Strangle: `strangle_data_fetcher.fetch_live_chain` now correctly uses
  `get_live_option_contracts` / `get_live_ltp` (previously it was calling
  the *expired*-instruments endpoint even in "live" mode — a second bug
  fixed as part of this).
- Forward-test forces `paper=True` in both paths — real market data, real
  strike/expiry/quote resolution, but `place_order()` is never called.
  `menu.py` labels this clearly and no longer asks for a date range, since
  forward-test is inherently "starting now," not a historical window.

## What to verify on your machine before trusting results

The endpoint paths for live (non-expired) option contracts
(`/v2/option/contract`, `/v3/market-quote/ltp`) are implemented per Upstox's
documented pattern but **you should confirm them against current Upstox API
docs** before running live — endpoint shapes drift, and unlike the
expired-instruments endpoints (which this project has already exercised
extensively), the live endpoints haven't been hit yet in this environment
(this sandbox can't reach `api.upstox.com`). Run `check_setup.py`, then a
small forward-test session, and read the `[DATA]` log lines to confirm real
rows are coming back before trusting a full run.
