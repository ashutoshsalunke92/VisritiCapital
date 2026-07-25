"""
Local on-disk cache for fetched option-leg minute data, keyed only by the
parameters that actually change WHICH CONTRACTS get pulled from Upstox
(underlying, date(s), strike_step, otm_wing_strikes).

Why this exists: entry_time, exit_time, per_leg_sl_pct, strategy_sl_pct,
strategy_tp_pct, hedge_leg_sl_pct do NOT change which candles need to be
fetched -- they only change how backtest_engine.py replays candles that
are already sitting in a DataFrame. So once a day's 4-leg minute data is
fetched once, you can re-run the backtest with a completely different set
of exit rules against the SAME cached data, instantly, with zero API calls.
This is what makes param_sweep.py practical (testing dozens of parameter
combinations without re-hitting Upstox, or waiting on your daily token).

For swing-trade mode specifically: the original fetch_swing_trade_data
trims candles to the exact entry_time..exit_time window BEFORE returning,
which would make the cache entry_time/exit_time-specific and defeat the
point. So this cache fetches swing data using a widened 09:15-15:30 window
regardless of your real params, and stores that. param_sweep.py then slices
whatever entry/exit datetime it needs out of the wide cached window itself.

Usage: swap
    from data_fetcher import fetch_day_data, fetch_swing_trade_data
for
    from data_cache import cached_fetch_day_data as fetch_day_data
    from data_cache import cached_fetch_swing_trade_data as fetch_swing_trade_data
in any script where you want caching (run_backtest.py, run_swing_backtest.py,
param_sweep.py already use it).
"""
import hashlib
import os
from dataclasses import replace

import pandas as pd

from config import StrategyParams
from upstox_client import UpstoxClient
import data_fetcher

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def _key(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _path(cache_key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{cache_key}.pkl")


def _save(df: pd.DataFrame, cache_key: str):
    # pickling preserves df.attrs (expiries/atm_strike/lot_size/entry_dt/exit_dt)
    # natively -- no sidecar file needed.
    df.to_pickle(_path(cache_key))


def _load(cache_key: str):
    p = _path(cache_key)
    if not os.path.isfile(p):
        return None
    return pd.read_pickle(p)


def cached_fetch_day_data(client: UpstoxClient, params: StrategyParams, trade_date) -> pd.DataFrame:
    cache_key = _key("day", params.underlying, trade_date.date().isoformat(),
                      params.strike_step, params.otm_wing_strikes)
    cached = _load(cache_key)
    if cached is not None:
        return cached
    df = data_fetcher.fetch_day_data(client, params, trade_date)
    _save(df, cache_key)
    return df


def cached_fetch_swing_trade_data(client: UpstoxClient, params: StrategyParams,
                                   entry_date, exit_date) -> pd.DataFrame:
    cache_key = _key("swing", params.underlying, entry_date.date().isoformat(),
                      exit_date.date().isoformat(), params.strike_step, params.otm_wing_strikes)
    cached = _load(cache_key)
    if cached is not None:
        return cached
    # Fetch the WIDEST plausible session window (market open to close) so the
    # cached data supports sweeping entry/exit TIME later, independent of
    # whatever params.entry_time/exit_time happen to be right now.
    wide_params = replace(params, entry_time="09:15", exit_time="15:30")
    df = data_fetcher.fetch_swing_trade_data(client, wide_params, entry_date, exit_date)
    _save(df, cache_key)
    return df


def clear_cache():
    """Wipes the local cache -- use if you change OTM_WING_STRIKES or
    STRIKE_STEP and the old cached data is now for the wrong contracts.
    (Changing those already changes the cache key automatically, so this
    is mostly for reclaiming disk space, not correctness.)"""
    import shutil
    if os.path.isdir(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
        print(f"Cleared {CACHE_DIR}")
    else:
        print("Cache already empty.")
