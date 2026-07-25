# NIFTY Iron Fly (4-Leg) — Upstox API Strategy + Backtester

Same strategy as before, now built against Upstox instead of Dhan (free
Data APIs vs Dhan's ₹499/month):

| Leg | Action | Instrument            | Expiry       | Strike            |
|-----|--------|------------------------|--------------|-------------------|
| 1   | SELL   | ATM Call               | Monthly      | ATM               |
| 2   | BUY    | OTM7 Call (hedge)      | Next Weekly  | ATM +7 strikes    |
| 3   | SELL   | ATM Put                | Monthly      | ATM               |
| 4   | BUY    | OTM7 Put (hedge)       | Next Weekly  | ATM -7 strikes    |

Entry 10:16, exit 14:59, intraday only. Exit rules: per-leg SL on the two
short legs, combined-strategy SL, combined-strategy TP, hard time exit.

**Read `TUNING_GUIDE.md` first if you're coming from the original version**
— it explains exactly what changed in this revision and why, plus new
tooling for testing SL/TP/entry-time choices against your own data.

## Why this can't run inside this chat

This sandbox can only reach a short allow-list of domains (GitHub, PyPI,
npm). `api.upstox.com` is not on it, so I can't fetch your data or place
orders from here. Everything below is real, runnable code — you run it on
your own machine or a small VPS where `api.upstox.com` is reachable.

## Project layout

```
upstox_iron_fly/
├── config.py            # loads credentials + strategy params from env/.env
├── upstox_client.py      # REST wrapper: expired-options data + order placement
├── data_fetcher.py       # resolves ATM strike, pulls expired-option minute data
├── data_cache.py         # NEW — local cache so re-running with different
│                         #        SL/TP/entry-time needs zero new API calls
├── strategy.py           # leg definitions, exit-rule state machine (broker-agnostic)
├── backtest_engine.py    # replays minute bars through strategy.py, produces trade log
├── run_backtest.py       # CLI entrypoint for intraday backtesting
├── run_swing_backtest.py # CLI entrypoint for swing/positional backtesting
├── param_sweep.py         # NEW — grid-search SL/TP/entry-time with train/test split
├── live_trader.py        # skeleton for live/paper trading (DISABLED by default)
├── debug_contracts.py    # one-off debug tool for inspecting raw Upstox responses
├── check_setup.py        # run this first if anything's not working
├── requirements.txt
├── .env.example
└── TUNING_GUIDE.md        # NEW — what changed and why, read this first
```

## Setup (on your own machine)

**1. Activate Upstox Plus** (needed for expired-options data — currently free):
web.upstox.com → Developer Apps → your app → make sure Plus is active.
Upstox says if it ever becomes chargeable they'll notify in advance.

**2. Generate an access token:**
Go to [developer.upstox.com](https://developer.upstox.com) → your app →
click **Generate** to create an access token.
⚠️ Upstox tokens expire daily (~3:30am IST) — you'll regenerate this every
morning before running a fresh backtest or live session. Once a date range
is cached (see `data_cache.py`), re-running `param_sweep.py` against it does
NOT need a fresh token — only the first fetch of new dates does.

**3. Install and configure:**
```powershell
cd upstox_iron_fly
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```
In Notepad, replace `UPSTOX_ACCESS_TOKEN=your_access_token_here` with your
real token. Save and close.

**Never paste your access token into a chat with me or anyone else.** It goes
only in your local `.env` file.

## Running a backtest

```powershell
python check_setup.py
python run_backtest.py --from 2025-06-01 --to 2025-06-30 --capital 400000
```

This will:
1. For each trading day, resolve current-month monthly expiry and
   next-week weekly expiry from Upstox's expired-instruments expiry list.
2. Resolve the ATM strike for that day (see caveat below).
3. Look up each leg's `expired_instrument_key` and pull its 1-minute candles.
4. Simulate entry at 10:16, apply the SL/TP/time-exit rules minute-by-minute,
   and log every trade.
5. Optionally skip low-credit days if `MIN_CREDIT_TO_WIDTH_PCT` is set (see
   `TUNING_GUIDE.md`).
6. Write `output/trades.csv` and print a summary (win rate, total P&L,
   max drawdown, avg P&L/day, close-reason breakdown).

### Finding better SL/TP/entry-time values

```powershell
python param_sweep.py --from 2025-06-01 --to 2026-07-15 --mode intraday
```

Grid-searches parameters against cached data with a train/test split so you
don't just curve-fit your own 6 months. See `TUNING_GUIDE.md` for how to
read the output.

### Known accuracy caveat — please read before trusting results

Upstox's 6-month expired-options depth is great, but its minute-level spot
price history is much shallower (roughly the last month, per their docs).
`data_fetcher.py` handles this by:
- Using precise 1-minute spot data to find the ATM strike when available
  (recent ~30 days).
- Falling back to the prior day's daily close as an approximation for older
  dates — which can occasionally be off by one strike step (50 points) if
  NIFTY gapped significantly overnight.

Practically: **backtest the most recent month first** — that's where ATM
strike selection is exact — and treat anything older as directionally
useful but slightly approximate. If the gap matters to you, the fix is
put-call-parity-based ATM detection (inferring ATM from where call and put
premiums converge, instead of from spot price) — let me know if you want
that built in; it removes the dependency on spot minute-data depth entirely.

## Going live later

`live_trader.py` reuses the exact same `strategy.py` state machine — the
only thing that changes is the data source (live feed instead of historical
candles) and that `upstox_client.place_order(...)` actually fires. It is
**disabled by default** (`DRY_RUN = True`) so nothing executes by accident.
Note live trading needs a *different* instrument-key resolution path than
backtesting (live option chain, not expired-instruments) — see the
docstring in `live_trader.py`.
