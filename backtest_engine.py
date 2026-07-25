"""
Replays a single day's minute-bar DataFrame (from data_fetcher.fetch_day_data)
through strategy.IronFlyState, using CLOSE price of each bar as the mark
price (entry itself uses the bar at/just-after entry_time's OPEN as fill
price — a simplifying assumption; real fills will differ by slippage).

CHANGE FROM ORIGINAL: passes params.hedge_leg_sl_pct through to IronFlyState
(both run_day and run_swing_trade). See strategy.py / config.py docstrings.
"""
from datetime import datetime, time as dtime
import pandas as pd

from strategy import IronFlyState
from config import StrategyParams


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def run_day(df: pd.DataFrame, params: StrategyParams, trade_date: datetime,
            quantity_override: int = None) -> dict:
    entry_t = _parse_hhmm(params.entry_time)
    exit_t = _parse_hhmm(params.exit_time)

    df = df.copy()
    df["time"] = df["datetime"].dt.time

    entry_rows = df[df["time"] >= entry_t]
    if entry_rows.empty:
        return {"date": trade_date.date().isoformat(), "status": "NO_DATA_AT_ENTRY"}
    entry_row = entry_rows.iloc[0]

    quantity = quantity_override if quantity_override is not None else params.quantity

    state = IronFlyState(
        quantity=quantity,
        per_leg_sl_pct=params.per_leg_sl_pct,
        strategy_sl_pct=params.strategy_sl_pct,
        strategy_tp_pct=params.strategy_tp_pct,
        hedge_leg_sl_pct=params.hedge_leg_sl_pct,
        sl_mode=getattr(params, "sl_mode", "STATIC"),
        trail_activate_pct=getattr(params, "trail_activate_pct", 0.5),
        trail_giveback_pct=getattr(params, "trail_giveback_pct", 0.3),
    )

    entry_prices = {
        "ce_atm": entry_row["ce_atm_open"],
        "pe_atm": entry_row["pe_atm_open"],
        "ce_otm7": entry_row["ce_otm7_open"],
        "pe_otm7": entry_row["pe_otm7_open"],
    }
    state.enter(entry_prices, entry_row["datetime"])

    remaining = df[df["datetime"] > entry_row["datetime"]]

    for _, row in remaining.iterrows():
        if state.closed:
            break
        marks = {
            "ce_atm": row["ce_atm_close"],
            "pe_atm": row["pe_atm_close"],
            "ce_otm7": row["ce_otm7_close"],
            "pe_otm7": row["pe_otm7_close"],
        }
        force = row["time"] >= exit_t
        state.on_tick(row["datetime"], marks, force_exit=force,
                       exit_reason="TIME_EXIT" if force else None)

    if not state.closed:
        last = df.iloc[-1]
        marks = {
            "ce_atm": last["ce_atm_close"], "pe_atm": last["pe_atm_close"],
            "ce_otm7": last["ce_otm7_close"], "pe_otm7": last["pe_otm7_close"],
        }
        state.on_tick(last["datetime"], marks, force_exit=True, exit_reason="EOD_DATA_END")

    pnl = state.total_realized_pnl()
    leg_rows = []
    for leg in state.legs.values():
        leg_rows.append({
            "date": trade_date.date().isoformat(),
            "leg": leg.name,
            "side": leg.side.value,
            "qty": leg.quantity,
            "entry_price": leg.entry_price,
            "exit_price": leg.exit_price,
            "exit_reason": leg.exit_reason,
            "leg_pnl": leg.realized_pnl(),
        })

    return {
        "date": trade_date.date().isoformat(),
        "status": "OK",
        "net_credit": state.net_credit,
        "close_reason": state.close_reason,
        "total_pnl": pnl,
        "legs": leg_rows,
    }


def run_swing_trade(df: pd.DataFrame, params: StrategyParams,
                     entry_dt: datetime, exit_dt: datetime,
                     quantity_override: int = None) -> dict:
    """Same exit-rule machinery as run_day, but entry/exit are full
    datetimes spanning multiple calendar days instead of a single day's
    time-of-day window. No daily square-off — only per-leg SL, combined
    SL/TP, or the final exit_dt force-close the position."""
    df = df.sort_values("datetime").reset_index(drop=True)

    entry_rows = df[df["datetime"] >= entry_dt]
    if entry_rows.empty:
        return {"entry_date": entry_dt.date().isoformat(), "status": "NO_DATA_AT_ENTRY"}
    entry_row = entry_rows.iloc[0]

    quantity = quantity_override if quantity_override is not None else params.quantity

    state = IronFlyState(
        quantity=quantity,
        per_leg_sl_pct=params.per_leg_sl_pct,
        strategy_sl_pct=params.strategy_sl_pct,
        strategy_tp_pct=params.strategy_tp_pct,
        hedge_leg_sl_pct=params.hedge_leg_sl_pct,
        sl_mode=getattr(params, "sl_mode", "STATIC"),
        trail_activate_pct=getattr(params, "trail_activate_pct", 0.5),
        trail_giveback_pct=getattr(params, "trail_giveback_pct", 0.3),
    )

    entry_prices = {
        "ce_atm": entry_row["ce_atm_open"],
        "pe_atm": entry_row["pe_atm_open"],
        "ce_otm7": entry_row["ce_otm7_open"],
        "pe_otm7": entry_row["pe_otm7_open"],
    }
    state.enter(entry_prices, entry_row["datetime"])

    remaining = df[df["datetime"] > entry_row["datetime"]]

    for _, row in remaining.iterrows():
        if state.closed:
            break
        marks = {
            "ce_atm": row["ce_atm_close"],
            "pe_atm": row["pe_atm_close"],
            "ce_otm7": row["ce_otm7_close"],
            "pe_otm7": row["pe_otm7_close"],
        }
        force = row["datetime"] >= exit_dt
        state.on_tick(row["datetime"], marks, force_exit=force,
                       exit_reason="TIME_EXIT" if force else None)

    if not state.closed:
        last = df.iloc[-1]
        marks = {
            "ce_atm": last["ce_atm_close"], "pe_atm": last["pe_atm_close"],
            "ce_otm7": last["ce_otm7_close"], "pe_otm7": last["pe_otm7_close"],
        }
        state.on_tick(last["datetime"], marks, force_exit=True, exit_reason="DATA_END")

    pnl = state.total_realized_pnl()
    leg_rows = []
    for leg in state.legs.values():
        leg_rows.append({
            "entry_date": entry_dt.date().isoformat(),
            "exit_date": exit_dt.date().isoformat(),
            "leg": leg.name,
            "side": leg.side.value,
            "qty": leg.quantity,
            "entry_price": leg.entry_price,
            "exit_price": leg.exit_price,
            "exit_reason": leg.exit_reason,
            "leg_pnl": leg.realized_pnl(),
        })

    return {
        "entry_date": entry_dt.date().isoformat(),
        "planned_exit_date": exit_dt.date().isoformat(),
        "status": "OK",
        "net_credit": state.net_credit,
        "close_reason": state.close_reason,
        "total_pnl": pnl,
        "legs": leg_rows,
    }
