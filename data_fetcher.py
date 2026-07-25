"""
Pulls the 4 legs' minute-level candle series for a given trading day (via
Upstox's Expired Instruments APIs) and aligns them into one DataFrame the
backtest engine can replay.

Unlike Dhan's rollingoption endpoint (which takes a strike LABEL like "ATM"
or "ATM+7"), Upstox works off literal expired_instrument_keys. So this
module has to do the strike resolution itself:

  1. Figure out the underlying's spot price at/around entry time on that day.
  2. Round to the nearest strike step -> that's the ATM strike.
  3. Look up each leg's expired_instrument_key from the contract list for
     that expiry.
  4. Pull 1-minute candles for each of those 4 instrument keys.

CAVEAT — spot price lookup depth: Upstox's minute-level historical candle
API is documented as only reliably covering roughly the last month for
1-minute data. For dates further back than that (which you'll hit often,
since Upstox gives you ~6 months of expired-options depth), the code below
falls back to using the previous trading day's daily close as an
approximation of the day's spot level. This is a real accuracy tradeoff:
recent-month backtests will have precise ATM strike selection; older months
may occasionally be off by one strike step if NIFTY gapped significantly
overnight. Treat results beyond ~30 days back as directional, not precise,
until we tighten this (e.g. via put-call-parity-based ATM detection, which
we can add if the approximation turns out to matter for your numbers).
"""
from datetime import datetime, timedelta
import pandas as pd

from upstox_client import UpstoxClient
from config import StrategyParams


def _candles_to_df(raw: dict) -> pd.DataFrame:
    """Upstox candle responses are a list of
    [timestamp, open, high, low, close, volume, oi] rows, most-recent-first."""
    candles = raw.get("data", {}).get("candles", [])
    if not candles:
        # BUGFIX: an empty DataFrame built via pd.DataFrame(columns=[...])
        # gives the "datetime" column dtype=object, not datetime64. Every
        # caller downstream does df["datetime"].dt.xxx, which then raises
        # "Can only use .dt accessor with datetimelike values" instead of
        # cleanly propagating "no data for this leg/day". This happened
        # intermittently whenever Upstox had zero 1-minute candles for a
        # thinly-traded deep-OTM contract (very common for OTM7 hedge legs).
        # Explicitly typing the empty columns fixes it — .dt calls on an
        # empty datetime64 series work fine and just produce an empty result,
        # which correctly flows into the existing "No option data returned"
        # checks in fetch_day_data/fetch_swing_trade_data.
        return pd.DataFrame({
            "datetime": pd.Series(dtype="datetime64[ns]"),
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
        })
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df = df.sort_values("datetime").reset_index(drop=True)
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def resolve_expiries(client: UpstoxClient, underlying: str, trade_date: datetime) -> dict:
    expiries = client.get_expiries(underlying)
    expiries = [datetime.strptime(e, "%Y-%m-%d") for e in expiries]

    weekly, monthly = [], []
    for e in expiries:
        same_month = [x for x in expiries if x.year == e.year and x.month == e.month]
        (monthly if e == max(same_month) else weekly).append(e)

    monthly_future = sorted([e for e in monthly if e.date() >= trade_date.date()])
    weekly_future = sorted([e for e in weekly if e.date() >= trade_date.date()])

    if not monthly_future:
        raise RuntimeError(f"No monthly expiry found on/after {trade_date.date()}")
    if len(weekly_future) < 2:
        raise RuntimeError(f"Not enough weekly expiries found on/after {trade_date.date()} "
                            f"(Upstox Plus only retains ~6mo of expired-instrument history — "
                            f"you may be near the edge of that window)")

    return {
        "monthly_expiry_date": monthly_future[0].date().isoformat(),
        "current_week_expiry_date": weekly_future[0].date().isoformat(),
        "next_week_expiry_date": weekly_future[1].date().isoformat(),
    }


def _get_spot_price_at_entry(client: UpstoxClient, params: StrategyParams, trade_date: datetime) -> float:
    date_str = trade_date.strftime("%Y-%m-%d")
    try:
        raw = client.get_spot_history(params.underlying, unit="minutes", interval=1,
                                       to_date=date_str, from_date=date_str)
        df = _candles_to_df(raw)
        entry_h, entry_m = map(int, params.entry_time.split(":"))
        target = trade_date.replace(hour=entry_h, minute=entry_m)
        at_or_after = df[df["datetime"] >= target]
        if not at_or_after.empty:
            return float(at_or_after.iloc[0]["open"])
    except Exception:
        pass

    # Fallback: previous day's daily close (see module caveat above)
    prev_day = trade_date - timedelta(days=5)  # wide enough to cross a weekend
    raw = client.get_spot_history(params.underlying, unit="days", interval=1,
                                   to_date=(trade_date - timedelta(days=1)).strftime("%Y-%m-%d"),
                                   from_date=prev_day.strftime("%Y-%m-%d"))
    df = _candles_to_df(raw)
    if df.empty:
        raise RuntimeError(f"Could not resolve spot price for {date_str} (neither minute nor daily data available)")
    return float(df.iloc[-1]["close"])


def _find_contract(contracts: list[dict], strike: float, option_type: str) -> dict:
    matches = [c for c in contracts
               if abs(float(c["strike_price"]) - strike) < 0.01
               and c["instrument_type"] == option_type]
    if not matches:
        raise RuntimeError(f"No contract found for strike={strike} type={option_type}")
    return matches[0]


def fetch_day_data(client: UpstoxClient, params: StrategyParams, trade_date: datetime) -> pd.DataFrame:
    date_str = trade_date.strftime("%Y-%m-%d")

    exp = resolve_expiries(client, params.underlying, trade_date)
    spot = _get_spot_price_at_entry(client, params, trade_date)
    atm_strike = round(spot / params.strike_step) * params.strike_step
    wing_offset = params.otm_wing_strikes * params.strike_step

    monthly_contracts = client.get_expired_option_contracts(params.underlying, exp["monthly_expiry_date"])
    weekly_contracts = client.get_expired_option_contracts(params.underlying, exp["next_week_expiry_date"])

    legs = {
        "ce_atm": _find_contract(monthly_contracts, atm_strike, "CE"),
        "pe_atm": _find_contract(monthly_contracts, atm_strike, "PE"),
        "ce_otm7": _find_contract(weekly_contracts, atm_strike + wing_offset, "CE"),
        "pe_otm7": _find_contract(weekly_contracts, atm_strike - wing_offset, "PE"),
    }

    lot_sizes = {name: int(c["lot_size"]) for name, c in legs.items()}
    if len(set(lot_sizes.values())) != 1:
        raise RuntimeError(f"Mismatched lot sizes across legs on {date_str}: {lot_sizes}")
    actual_lot_size = next(iter(lot_sizes.values()))

    merged = None
    for leg_name, contract in legs.items():
        raw = client.get_expired_historical_candles(
            expired_instrument_key=contract["instrument_key"],
            interval="1minute", to_date=date_str, from_date=date_str,
        )
        df = _candles_to_df(raw)
        df = df[df["datetime"].dt.date == trade_date.date()]
        df = df.rename(columns={c: f"{leg_name}_{c}" for c in ["open", "high", "low", "close", "volume"]})
        merged = df if merged is None else pd.merge(merged, df, on="datetime", how="inner")

    if merged is None or merged.empty:
        raise RuntimeError(f"No option data returned for {date_str}")

    merged = merged.sort_values("datetime").reset_index(drop=True)
    merged.attrs["expiries"] = exp
    merged.attrs["atm_strike"] = atm_strike
    merged.attrs["lot_size"] = actual_lot_size
    return merged


# ---------------------------------------------------------------------------
# Swing-trade mode: enter Wednesday 10:16 (day after Tuesday's weekly expiry),
# hold through Monday 14:59 (or until an SL/TP rule fires earlier). Same 4
# legs, same contracts, just no more daily square-off.
# ---------------------------------------------------------------------------

def resolve_swing_expiries(client: UpstoxClient, underlying: str, entry_date: datetime) -> dict:
    """For a Wednesday entry (the day after Tuesday's weekly expiry), the
    nearest future weekly expiry IS the correct 'next weekly' — unlike the
    intraday version, there's no earlier same-week expiry left to skip over."""
    expiries = client.get_expiries(underlying)
    expiries = [datetime.strptime(e, "%Y-%m-%d") for e in expiries]

    weekly, monthly = [], []
    for e in expiries:
        same_month = [x for x in expiries if x.year == e.year and x.month == e.month]
        (monthly if e == max(same_month) else weekly).append(e)

    monthly_future = sorted([e for e in monthly if e.date() >= entry_date.date()])
    weekly_future = sorted([e for e in weekly if e.date() >= entry_date.date()])

    if not monthly_future:
        raise RuntimeError(f"No monthly expiry found on/after {entry_date.date()}")
    if not weekly_future:
        raise RuntimeError(f"No weekly expiry found on/after {entry_date.date()} "
                            f"(near the edge of Upstox's expired-instrument history window)")

    return {
        "monthly_expiry_date": monthly_future[0].date().isoformat(),
        "weekly_expiry_date": weekly_future[0].date().isoformat(),
    }


def fetch_swing_trade_data(client: UpstoxClient, params: StrategyParams,
                            entry_date: datetime, exit_date: datetime) -> pd.DataFrame:
    """Pulls minute candles for all 4 legs across the full entry_date..exit_date
    span (e.g. Wednesday through Monday) in a single API call per leg, then
    trims to the exact entry-time..exit-time window."""
    entry_h, entry_m = map(int, params.entry_time.split(":"))
    exit_h, exit_m = map(int, params.exit_time.split(":"))
    entry_dt = entry_date.replace(hour=entry_h, minute=entry_m)
    exit_dt = exit_date.replace(hour=exit_h, minute=exit_m)

    exp = resolve_swing_expiries(client, params.underlying, entry_date)

    monthly_expiry_dt = datetime.strptime(exp["monthly_expiry_date"], "%Y-%m-%d")
    if monthly_expiry_dt.date() < exit_date.date():
        raise RuntimeError(
            f"Monthly ATM legs (expiry {exp['monthly_expiry_date']}) would expire "
            f"before the planned exit ({exit_date.date()}) — this swing window "
            f"straddles monthly expiry, skipping."
        )

    spot = _get_spot_price_at_entry(client, params, entry_date)
    atm_strike = round(spot / params.strike_step) * params.strike_step
    wing_offset = params.otm_wing_strikes * params.strike_step

    monthly_contracts = client.get_expired_option_contracts(params.underlying, exp["monthly_expiry_date"])
    weekly_contracts = client.get_expired_option_contracts(params.underlying, exp["weekly_expiry_date"])

    legs = {
        "ce_atm": _find_contract(monthly_contracts, atm_strike, "CE"),
        "pe_atm": _find_contract(monthly_contracts, atm_strike, "PE"),
        "ce_otm7": _find_contract(weekly_contracts, atm_strike + wing_offset, "CE"),
        "pe_otm7": _find_contract(weekly_contracts, atm_strike - wing_offset, "PE"),
    }

    lot_sizes = {name: int(c["lot_size"]) for name, c in legs.items()}
    if len(set(lot_sizes.values())) != 1:
        raise RuntimeError(f"Mismatched lot sizes across legs entering {entry_date.date()}: {lot_sizes}")
    actual_lot_size = next(iter(lot_sizes.values()))

    merged = None
    for leg_name, contract in legs.items():
        raw = client.get_expired_historical_candles(
            expired_instrument_key=contract["instrument_key"],
            interval="1minute",
            to_date=exit_date.strftime("%Y-%m-%d"),
            from_date=entry_date.strftime("%Y-%m-%d"),
        )
        df = _candles_to_df(raw)
        df = df[(df["datetime"] >= entry_dt) & (df["datetime"] <= exit_dt)]
        df = df.rename(columns={c: f"{leg_name}_{c}" for c in ["open", "high", "low", "close", "volume"]})
        merged = df if merged is None else pd.merge(merged, df, on="datetime", how="inner")

    if merged is None or merged.empty:
        raise RuntimeError(f"No option data returned for swing window "
                            f"{entry_date.date()}..{exit_date.date()}")

    merged = merged.sort_values("datetime").reset_index(drop=True)
    merged.attrs["expiries"] = exp
    merged.attrs["atm_strike"] = atm_strike
    merged.attrs["lot_size"] = actual_lot_size
    merged.attrs["entry_dt"] = entry_dt
    merged.attrs["exit_dt"] = exit_dt
    return merged
