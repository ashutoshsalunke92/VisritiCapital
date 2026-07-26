"""
Replays a single day's minute-bar DataFrame through strategy.IronFlyState.

CHANGE IN THIS REVISION:
  run_day() and run_swing_trade() now accept an optional `exit_engine`
  argument (a TradeExitEngine instance). When supplied, each tick goes
  through the engine instead of IronFlyState.on_tick():
    1. Build TradeContext from current marks.
    2. Call exit_engine.evaluate_trade(context) -> Decision.
    3. Apply the decision (close_all or close_leg) on the state object.
  When exit_engine is None (default), the original on_tick() path runs
  unchanged — run_backtest.py and run_swing_backtest.py pass no engine
  and continue to work exactly as before.
"""
from datetime import datetime, time as dtime
from typing import Optional
import pandas as pd

from strategy import IronFlyState
from config import StrategyParams


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _apply_decision(state: IronFlyState, decision, prices: dict, ts):
    """Applies a Decision returned by the exit engine to the IronFlyState."""
    from exit_engine.models import ExitAction
    if decision.continue_trade:
        return
    if not decision.leg_decisions or decision.close_entire_trade:
        state.close_all(prices, decision.exit_reason.value, ts)
        return
    for ld in decision.leg_decisions:
        if ld.action in (ExitAction.EXIT_LEG, ExitAction.CLOSE_ENTIRE_TRADE):
            state.close_leg(ld.leg_id, prices.get(ld.leg_id, 0.0), ld.reason.value, ts)
    if not state.open_legs():
        state.closed = True
        state.close_reason = decision.exit_reason.value


def _build_trade_context(state: IronFlyState, marks: dict, ts,
                          capital_allocated: float):
    """Builds a TradeContext from current IronFlyState for exit engine evaluation."""
    from exit_engine.models import (
        Trade, Position, Leg as ELeg, TradeContext,
        TradeState, new_trade_id, new_position_id, new_leg_id,
    )
    # Map IronFlyState legs to exit_engine Leg objects
    position = Position(position_id=new_position_id())
    for name, leg in state.legs.items():
        if not leg.is_open:
            continue
        elg = ELeg(
            leg_id=name,              # use leg name as ID for simplicity
            name=name,
            side=leg.side.value,
            quantity=leg.quantity,
            entry_premium=leg.entry_price or 0.0,
            current_premium=marks.get(name, leg.entry_price or 0.0),
        )
        position.legs[name] = elg

    trade = Trade(
        trade_id=getattr(state, "_trade_id", new_trade_id()),
        strategy_id="iron_fly",
        position=position,
        state=TradeState.ACTIVE,
        entry_premium=state.net_credit,
        capital_allocated=capital_allocated,
    )
    # Sync realized PnL into the trade object
    trade.realized_pnl = sum(
        leg.realized_pnl() for leg in state.legs.values() if not leg.is_open
    )

    return TradeContext(trade=trade, leg_marks=marks, as_of=ts)


def run_day(df: pd.DataFrame, params: StrategyParams, trade_date: datetime,
            quantity_override: int = None, exit_engine=None) -> dict:
    entry_t = _parse_hhmm(params.entry_time)
    exit_t  = _parse_hhmm(params.exit_time)

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
        "ce_atm":  entry_row["ce_atm_open"],
        "pe_atm":  entry_row["pe_atm_open"],
        "ce_otm7": entry_row["ce_otm7_open"],
        "pe_otm7": entry_row["pe_otm7_open"],
    }
    state.enter(entry_prices, entry_row["datetime"])

    capital = getattr(params, "capital_deployed", 250000.0)
    remaining = df[df["datetime"] > entry_row["datetime"]]

    for _, row in remaining.iterrows():
        if state.closed:
            break
        marks = {
            "ce_atm":  row["ce_atm_close"],
            "pe_atm":  row["pe_atm_close"],
            "ce_otm7": row["ce_otm7_close"],
            "pe_otm7": row["pe_otm7_close"],
        }
        force = row["time"] >= exit_t

        if force:
            state.close_all(marks, "TIME_EXIT", row["datetime"])
        elif exit_engine is not None:
            ctx = _build_trade_context(state, marks, row["datetime"], capital)
            decision = exit_engine.evaluate_trade(ctx)
            _apply_decision(state, decision, marks, row["datetime"])
        else:
            state.on_tick(row["datetime"], marks)

    if not state.closed:
        last = df.iloc[-1]
        marks = {
            "ce_atm":  last["ce_atm_close"], "pe_atm":  last["pe_atm_close"],
            "ce_otm7": last["ce_otm7_close"], "pe_otm7": last["pe_otm7_close"],
        }
        state.close_all(marks, "EOD_DATA_END", last["datetime"])

    pnl = state.total_realized_pnl()
    leg_rows = []
    for leg in state.legs.values():
        leg_rows.append({
            "date":        trade_date.date().isoformat(),
            "leg":         leg.name,
            "side":        leg.side.value,
            "qty":         leg.quantity,
            "entry_price": leg.entry_price,
            "exit_price":  leg.exit_price,
            "exit_reason": leg.exit_reason,
            "leg_pnl":     leg.realized_pnl(),
        })

    return {
        "date":         trade_date.date().isoformat(),
        "status":       "OK",
        "net_credit":   state.net_credit,
        "close_reason": state.close_reason,
        "total_pnl":    pnl,
        "legs":         leg_rows,
    }


def run_swing_trade(df: pd.DataFrame, params: StrategyParams,
                    entry_dt: datetime, exit_dt: datetime,
                    quantity_override: int = None, exit_engine=None) -> dict:
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
        "ce_atm":  entry_row["ce_atm_open"],
        "pe_atm":  entry_row["pe_atm_open"],
        "ce_otm7": entry_row["ce_otm7_open"],
        "pe_otm7": entry_row["pe_otm7_open"],
    }
    state.enter(entry_prices, entry_row["datetime"])

    capital = getattr(params, "capital_deployed", 250000.0)
    remaining = df[df["datetime"] > entry_row["datetime"]]

    for _, row in remaining.iterrows():
        if state.closed:
            break
        marks = {
            "ce_atm":  row["ce_atm_close"],
            "pe_atm":  row["pe_atm_close"],
            "ce_otm7": row["ce_otm7_close"],
            "pe_otm7": row["pe_otm7_close"],
        }
        force = row["datetime"] >= exit_dt

        if force:
            state.close_all(marks, "TIME_EXIT", row["datetime"])
        elif exit_engine is not None:
            ctx = _build_trade_context(state, marks, row["datetime"], capital)
            decision = exit_engine.evaluate_trade(ctx)
            _apply_decision(state, decision, marks, row["datetime"])
        else:
            state.on_tick(row["datetime"], marks)

    if not state.closed:
        last = df.iloc[-1]
        marks = {
            "ce_atm":  last["ce_atm_close"], "pe_atm":  last["pe_atm_close"],
            "ce_otm7": last["ce_otm7_close"], "pe_otm7": last["pe_otm7_close"],
        }
        state.close_all(marks, "DATA_END", last["datetime"])

    pnl = state.total_realized_pnl()
    leg_rows = []
    for leg in state.legs.values():
        leg_rows.append({
            "entry_date":  entry_dt.date().isoformat(),
            "exit_date":   exit_dt.date().isoformat(),
            "leg":         leg.name,
            "side":        leg.side.value,
            "qty":         leg.quantity,
            "entry_price": leg.entry_price,
            "exit_price":  leg.exit_price,
            "exit_reason": leg.exit_reason,
            "leg_pnl":     leg.realized_pnl(),
        })

    return {
        "entry_date":         entry_dt.date().isoformat(),
        "planned_exit_date":  exit_dt.date().isoformat(),
        "status":             "OK",
        "net_credit":         state.net_credit,
        "close_reason":       state.close_reason,
        "total_pnl":          pnl,
        "legs":               leg_rows,
    }
