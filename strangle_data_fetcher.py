"""
Data fetcher for the Hedged 25-Delta Strangle.

FIXES IN THIS REVISION (see TUNING_GUIDE_V2.md):
  - _resolve_expiries(): now requires TWO future weekly expiries and uses
    weekly_future[1] for the hedge leg, exactly like the Iron Fly's
    data_fetcher.resolve_expiries(). Previously this used weekly_future[0]
    (the NEAREST weekly), which is very often 0-4 DTE -- degenerate IV
    solves, deltas collapsing to 0/1, hedge selection failing or picking
    nonsense strikes. This was likely the single biggest reason the
    strangle looked broken.
  - Strike window bound: only strikes within `strike_window` steps of ATM
    are ever fetched (previously the ENTIRE contract list at that expiry —
    often 100+ strikes — was pulled every single replay tick).
  - Day-level caching: each instrument's full day of 1-minute candles is
    fetched ONCE and cached (in-memory + on-disk, same .cache/ convention
    as data_cache.py), then every replay tick slices that cached series
    in-memory. This turns what was "N API calls per 5-minute tick" into
    "N API calls per trading day, total" — the fix for the effective
    rate-limiting/timeout death the strangle was hitting before.
  - Every fetch is logged with the real instrument_key and candle count
    actually returned by Upstox, so it's auditable that data is real
    (never fabricated) -- see verify_real_data().

Two entry points, unchanged in shape:
  - fetch_chain_at_datetime: backtest / forward-test replay (historical
    expired-instrument data).
  - fetch_live_chain: live trading / forward-test's live leg (live
    option-chain quotes) -- see live_data_source.py for how forward-test
    now actually uses this instead of historical replay (fix #8).
"""
import hashlib
import os
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

from upstox_client import UpstoxClient, UNDERLYING_INSTRUMENT_KEYS
from strike_selector import ChainRow, OptionChain

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_THIS_DIR, ".cache")


# ---------------------------------------------------------------------------
# Day-level candle series cache (the performance fix)
# ---------------------------------------------------------------------------

def _cache_key(instrument_key: str, date_str: str) -> str:
    return hashlib.sha1(f"strangle_leg|{instrument_key}|{date_str}".encode()).hexdigest()[:16]


def _cache_path(key: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{key}.pkl")


_MEMORY_CACHE: dict = {}   # per-process cache, avoids re-hitting disk within one run


def _fetch_day_series(client: UpstoxClient, instrument_key: str, date_str: str) -> pd.DataFrame:
    """Fetches (and caches) ALL of one instrument's 1-minute candles for one
    trading day, in a SINGLE API call. Every subsequent price lookup for that
    instrument on that day slices this in memory -- zero additional calls."""
    mem_key = (instrument_key, date_str)
    if mem_key in _MEMORY_CACHE:
        return _MEMORY_CACHE[mem_key]

    disk_key = _cache_key(instrument_key, date_str)
    disk_path = _cache_path(disk_key)
    if os.path.isfile(disk_path):
        df = pd.read_pickle(disk_path)
        _MEMORY_CACHE[mem_key] = df
        return df

    raw = client.get_expired_historical_candles(
        expired_instrument_key=instrument_key, interval="1minute",
        to_date=date_str, from_date=date_str,
    )
    df = _parse_candle_df(raw)
    print(f"[DATA] Upstox real candles: {instrument_key}  {date_str}  rows={len(df)}")
    df.to_pickle(disk_path)
    _MEMORY_CACHE[mem_key] = df
    return df


def _price_at_or_before(df: pd.DataFrame, at_dt: datetime) -> Optional[float]:
    if df.empty:
        return None
    day_rows = df[df["datetime"].dt.date == at_dt.date()]
    at_or_before = day_rows[day_rows["datetime"] <= at_dt]
    if at_or_before.empty:
        return None
    return float(at_or_before.iloc[-1]["close"])


def verify_real_data(df: pd.DataFrame, instrument_key: str) -> bool:
    """Sanity check: real Upstox data always has a real timestamp column and
    at least one row with a positive close. Never used to fabricate a value
    -- only to confirm what came back is genuine before trusting it."""
    if df is None or df.empty:
        return False
    if "close" not in df.columns or "datetime" not in df.columns:
        return False
    return bool((df["close"] > 0).any())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_candle_df(raw: dict) -> pd.DataFrame:
    candles = raw.get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame({
            "datetime": pd.Series(dtype="datetime64[ns]"),
            "open": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
        })
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    return df.sort_values("datetime").reset_index(drop=True)[["datetime", "open", "close"]]


def _get_spot(client: UpstoxClient, underlying: str, at_dt: datetime) -> float:
    date_str = at_dt.strftime("%Y-%m-%d")
    try:
        raw = client.get_spot_history(underlying, unit="minutes", interval=1,
                                       to_date=date_str, from_date=date_str)
        df = _parse_candle_df(raw)
        at_or_after = df[df["datetime"] >= at_dt]
        if not at_or_after.empty:
            return float(at_or_after.iloc[0]["open"])
    except Exception:
        pass
    prev = at_dt - timedelta(days=5)
    raw = client.get_spot_history(underlying, unit="days", interval=1,
                                   to_date=(at_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
                                   from_date=prev.strftime("%Y-%m-%d"))
    df = _parse_candle_df(raw)
    if df.empty:
        raise RuntimeError(f"Cannot resolve spot for {date_str}")
    return float(df.iloc[-1]["close"])


def _resolve_expiries(client: UpstoxClient, underlying: str, trade_date: datetime):
    """FIXED: mirrors data_fetcher.resolve_expiries exactly -- requires TWO
    future weekly expiries and skips to weekly_future[1] for the hedge leg,
    so the hedge is never built off a 0-4 DTE contract."""
    expiries = client.get_expiries(underlying)
    expiries_dt = [datetime.strptime(e, "%Y-%m-%d") for e in expiries]

    weekly, monthly = [], []
    for e in expiries_dt:
        same_month = [x for x in expiries_dt if x.year == e.year and x.month == e.month]
        (monthly if e == max(same_month) else weekly).append(e)

    monthly_future = sorted([e for e in monthly if e.date() >= trade_date.date()])
    weekly_future = sorted([e for e in weekly if e.date() >= trade_date.date()])

    if not monthly_future:
        raise RuntimeError(f"No monthly expiry found on/after {trade_date.date()}")
    if len(weekly_future) < 2:
        raise RuntimeError(f"Not enough weekly expiries found on/after {trade_date.date()} "
                            f"(near the edge of Upstox's expired-instrument history window)")
    # short (delta-25) leg -> nearest monthly. hedge (delta-5) leg -> the
    # SECOND future weekly, not the nearest one (which is often too close to
    # expiry to be a usable, liquid, non-degenerate hedge).
    return monthly_future[0].strftime("%Y-%m-%d"), weekly_future[1].strftime("%Y-%m-%d")


def resolve_live_expiries(client: UpstoxClient, underlying: str, as_of: datetime):
    """Live equivalent of _resolve_expiries() -- same skip-the-nearest-weekly
    logic, sourced from get_live_expiries() (tradable expiries) instead of
    the expired-instruments list."""
    expiries = client.get_live_expiries(underlying)
    expiries_dt = [datetime.strptime(e, "%Y-%m-%d") for e in expiries]

    weekly, monthly = [], []
    for e in expiries_dt:
        same_month = [x for x in expiries_dt if x.year == e.year and x.month == e.month]
        (monthly if e == max(same_month) else weekly).append(e)

    monthly_future = sorted([e for e in monthly if e.date() >= as_of.date()])
    weekly_future = sorted([e for e in weekly if e.date() >= as_of.date()])

    if not monthly_future:
        raise RuntimeError(f"No live monthly expiry found on/after {as_of.date()}")
    if len(weekly_future) < 2:
        raise RuntimeError(f"Not enough live weekly expiries found on/after {as_of.date()}")
    return monthly_future[0].strftime("%Y-%m-%d"), weekly_future[1].strftime("%Y-%m-%d")


def _days_to_expiry(expiry_str: str, as_of: datetime) -> float:
    expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d")
    return max((expiry_dt - as_of).total_seconds() / 86400.0, 0.0)


def _bounded_contracts(contracts: list, spot: float, strike_step: int, strike_window: int) -> list:
    """FIXED: bounds the contract list to `strike_window` strikes on each
    side of ATM BEFORE any candle data is fetched -- this is what turns a
    100+ strike full-chain scan into ~2*strike_window+1 strikes."""
    atm = round(spot / strike_step) * strike_step
    lo, hi = atm - strike_window * strike_step, atm + strike_window * strike_step
    return [c for c in contracts if lo <= float(c["strike_price"]) <= hi]


def _build_chain_from_contracts(contracts: list, client: UpstoxClient, at_dt: datetime,
                                 strike_step: int, spot: float, strike_window: int,
                                 is_historical: bool) -> list:
    date_str = at_dt.strftime("%Y-%m-%d")
    rows_by_strike = {}

    bounded = _bounded_contracts(contracts, spot, strike_step, strike_window) if is_historical else contracts

    for c in bounded:
        strike = float(c["strike_price"])
        opt_type = c["instrument_type"]
        if opt_type not in ("CE", "PE"):
            continue
        try:
            if is_historical:
                day_df = _fetch_day_series(client, c["instrument_key"], date_str)
                if not verify_real_data(day_df, c["instrument_key"]):
                    continue
                premium = _price_at_or_before(day_df, at_dt)
                if premium is None:
                    continue
            else:
                premium = c.get("_ltp")
                if premium is None:
                    continue
        except Exception:
            continue

        if premium is None or premium <= 0:
            continue
        if strike not in rows_by_strike:
            rows_by_strike[strike] = {"CE": 0.0, "PE": 0.0}
        rows_by_strike[strike][opt_type] = premium

    return [
        ChainRow(strike=s, ce_premium=v["CE"], pe_premium=v["PE"])
        for s, v in sorted(rows_by_strike.items())
        if v["CE"] > 0 and v["PE"] > 0
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_chain_at_datetime(client: UpstoxClient, underlying: str, strike_step: int,
                             at_dt: datetime,
                             short_expiry: Optional[str] = None,
                             hedge_expiry: Optional[str] = None,
                             strike_window: int = 20) -> tuple:
    spot = _get_spot(client, underlying, at_dt)
    if short_expiry is None or hedge_expiry is None:
        short_expiry, hedge_expiry = _resolve_expiries(client, underlying, at_dt)

    short_contracts = client.get_expired_option_contracts(underlying, short_expiry)
    hedge_contracts = client.get_expired_option_contracts(underlying, hedge_expiry)

    short_rows = _build_chain_from_contracts(short_contracts, client, at_dt, strike_step,
                                              spot, strike_window, is_historical=True)
    hedge_rows = _build_chain_from_contracts(hedge_contracts, client, at_dt, strike_step,
                                              spot, strike_window, is_historical=True)

    if not short_rows:
        raise RuntimeError(f"No usable short-leg chain rows for {underlying} "
                            f"expiry={short_expiry} at {at_dt} (real Upstox data, "
                            f"none within the fetched strike window)")
    if not hedge_rows:
        raise RuntimeError(f"No usable hedge-leg chain rows for {underlying} "
                            f"expiry={hedge_expiry} at {at_dt}")

    short_dte = _days_to_expiry(short_expiry, at_dt)
    hedge_dte = _days_to_expiry(hedge_expiry, at_dt)

    short_chain = OptionChain(underlying=underlying, expiry_date=short_expiry,
                               spot=spot, t_years=short_dte / 365.0,
                               rows=short_rows, strike_step=strike_step)
    hedge_chain = OptionChain(underlying=underlying, expiry_date=hedge_expiry,
                               spot=spot, t_years=hedge_dte / 365.0,
                               rows=hedge_rows, strike_step=strike_step)
    return short_chain, hedge_chain


def fetch_live_chain(client: UpstoxClient, underlying: str, strike_step: int,
                      short_expiry: str, hedge_expiry: str, strike_window: int = 20) -> tuple:
    """Live option-chain quotes -- used for real live trading AND for
    forward-test (fix #8: forward-test now runs this, real live market data,
    not a historical replay).

    FIXED in this revision: previously called get_expired_option_contracts()
    even here, which returns EXPIRED instrument keys that can't be quoted or
    traded live. Now uses get_live_option_contracts() / get_live_ltp()."""
    spot_map = client.get_live_ltp([UNDERLYING_INSTRUMENT_KEYS[underlying]])
    spot = float(spot_map.get(UNDERLYING_INSTRUMENT_KEYS[underlying], 0.0))
    if spot <= 0:
        raise RuntimeError(f"Live spot price for {underlying} returned 0 — check market hours / token")

    now = datetime.now()
    short_contracts = client.get_live_option_contracts(underlying, short_expiry)
    hedge_contracts = client.get_live_option_contracts(underlying, hedge_expiry)
    short_contracts = _bounded_contracts(short_contracts, spot, strike_step, strike_window)
    hedge_contracts = _bounded_contracts(hedge_contracts, spot, strike_step, strike_window)

    short_ltp = client.get_live_ltp([c["instrument_key"] for c in short_contracts])
    hedge_ltp = client.get_live_ltp([c["instrument_key"] for c in hedge_contracts])
    for c in short_contracts:
        c["_ltp"] = short_ltp.get(c["instrument_key"], 0.0)
    for c in hedge_contracts:
        c["_ltp"] = hedge_ltp.get(c["instrument_key"], 0.0)

    print(f"[DATA] Upstox LIVE quotes: {underlying} spot={spot}  "
          f"short_contracts={len(short_contracts)}  hedge_contracts={len(hedge_contracts)}")

    short_rows = _build_chain_from_contracts(short_contracts, client, now, strike_step,
                                              spot, strike_window, is_historical=False)
    hedge_rows = _build_chain_from_contracts(hedge_contracts, client, now, strike_step,
                                              spot, strike_window, is_historical=False)

    short_dte = _days_to_expiry(short_expiry, now)
    hedge_dte = _days_to_expiry(hedge_expiry, now)
    short_chain = OptionChain(underlying=underlying, expiry_date=short_expiry,
                               spot=spot, t_years=short_dte / 365.0,
                               rows=short_rows, strike_step=strike_step)
    hedge_chain = OptionChain(underlying=underlying, expiry_date=hedge_expiry,
                               spot=spot, t_years=hedge_dte / 365.0,
                               rows=hedge_rows, strike_step=strike_step)
    return short_chain, hedge_chain
