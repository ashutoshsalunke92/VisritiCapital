"""
NEW module. Resolves the Iron Fly's 4 real, TRADABLE (not expired) legs for
right now, and their live LTPs -- this is what fix #8 needs: forward-test
must run against the actual live market (real quotes, real strike/expiry
resolution), just with orders simulated instead of sent, not a relabeled
historical replay.

Mirrors data_fetcher.py's ATM+OTM7 structure exactly (same strike/expiry
rules: short legs ATM on the nearest monthly, hedge legs OTM_WING_STRIKES
out on the next weekly, skipping the too-near current week) but sourced
from Upstox's LIVE option-contract + LTP endpoints instead of the
expired-instruments endpoints used for backtesting.
"""
from datetime import datetime

from upstox_client import UpstoxClient, UNDERLYING_INSTRUMENT_KEYS
from config import StrategyParams


def _resolve_live_expiries(client: UpstoxClient, underlying: str, as_of: datetime) -> dict:
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

    return {
        "monthly_expiry_date": monthly_future[0].strftime("%Y-%m-%d"),
        "next_week_expiry_date": weekly_future[1].strftime("%Y-%m-%d"),
    }


def _find_contract(contracts: list, strike: float, option_type: str) -> dict:
    matches = [c for c in contracts if abs(float(c["strike_price"]) - strike) < 0.01
               and c["instrument_type"] == option_type]
    if not matches:
        raise RuntimeError(f"No LIVE contract found for strike={strike} type={option_type}")
    return matches[0]


def resolve_live_ironfly_legs(client: UpstoxClient, params: StrategyParams) -> dict:
    """Returns {'ce_atm': {'instrument_key':..,'strike':..,'ltp':..}, ...}
    for the 4 real, currently tradable Iron Fly legs, using a real live spot
    quote to pick ATM -- same structure as data_fetcher.fetch_day_data, just
    live instead of historical."""
    now = datetime.now()
    exp = _resolve_live_expiries(client, params.underlying, now)

    spot_map = client.get_live_ltp([UNDERLYING_INSTRUMENT_KEYS[params.underlying]])
    spot = float(spot_map.get(UNDERLYING_INSTRUMENT_KEYS[params.underlying], 0.0))
    if spot <= 0:
        raise RuntimeError(f"Live spot for {params.underlying} returned 0 — market may be closed")

    atm_strike = round(spot / params.strike_step) * params.strike_step
    wing_offset = params.otm_wing_strikes * params.strike_step

    monthly_contracts = client.get_live_option_contracts(params.underlying, exp["monthly_expiry_date"])
    weekly_contracts = client.get_live_option_contracts(params.underlying, exp["next_week_expiry_date"])

    legs = {
        "ce_atm": _find_contract(monthly_contracts, atm_strike, "CE"),
        "pe_atm": _find_contract(monthly_contracts, atm_strike, "PE"),
        "ce_otm7": _find_contract(weekly_contracts, atm_strike + wing_offset, "CE"),
        "pe_otm7": _find_contract(weekly_contracts, atm_strike - wing_offset, "PE"),
    }

    keys = [c["instrument_key"] for c in legs.values()]
    ltp_map = client.get_live_ltp(keys)

    print(f"[DATA] Upstox LIVE quotes: {params.underlying} spot={spot} atm={atm_strike} "
          f"legs={ {n: c['instrument_key'] for n, c in legs.items()} }")

    out = {}
    for name, c in legs.items():
        ltp = float(ltp_map.get(c["instrument_key"], 0.0))
        if ltp <= 0:
            raise RuntimeError(f"LIVE LTP for {name} ({c['instrument_key']}) returned 0 — "
                                f"refusing to fabricate a fill price.")
        out[name] = {"instrument_key": c["instrument_key"], "strike": float(c["strike_price"]), "ltp": ltp}

    lot_sizes = {name: int(c["lot_size"]) for name, c in legs.items()}
    actual_lot_size = next(iter(set(lot_sizes.values())))
    if len(set(lot_sizes.values())) != 1:
        raise RuntimeError(f"Mismatched live lot sizes across legs: {lot_sizes}")

    return {"legs": out, "lot_size": actual_lot_size, "spot": spot, "expiries": exp, "ts": now}
