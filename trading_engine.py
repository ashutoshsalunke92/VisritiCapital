"""
Ties every module together and is what menu.py calls. One RunConfig, one
run_session() entrypoint, dispatches by strategy x mode x timeframe.

FIXES IN THIS REVISION (see TUNING_GUIDE_V2.md for the full writeup):
  #3  No date prompts. Backtest/forward-test always use a fixed rolling
      window (BACKTEST_WINDOW_DAYS in .env/config.py, default 180 days back
      from today) unless date_from/date_to are explicitly passed in (kept
      as optional RunConfig fields for future CLI/API use -- the *menu*
      never asks for them anymore).
  #4  out_dir is always "output" -- RunConfig.out_dir is no longer surfaced
      by the menu.
  #5  capital is always ₹250,000 per runner (RunConfig.capital defaults from
      config.load_session_defaults().fixed_capital) -- no longer surfaced by
      the menu.
  #6  Iron Condor monthly is now genuinely different from weekly: separate
      non-overlapping entry/exit cycles (exit = monthly expiry minus
      IRON_CONDOR_MONTHLY_EXIT_DAYS_BEFORE_EXPIRY, default 14 days) and its
      own SL/TP thresholds (StrategyParams.resolved_monthly_sl_pct/tp_pct,
      default 2x the weekly ones). The underlying data_fetcher.py /
      strategy.py / backtest_engine.py are completely untouched -- only the
      entry/exit DATES and the SL/TP params passed in differ.
  #7  Strangle chain building now goes through the fixed strangle_data_fetcher
      (correct next-weekly skip, bounded strike window, day-level caching,
      logged real-data provenance) -- see that module's docstring.
  #8  Forward-test now runs against REAL LIVE market data (live_data_source.py
      for Iron Fly, strangle_data_fetcher.fetch_live_chain for the Strangle),
      with orders simulated (never placed) -- not a historical replay
      relabeled as "forward test".
"""
import os
from dataclasses import dataclass, replace
from datetime import datetime, date, timedelta, time as dtime
from typing import Optional
import pandas as pd

from config import (load_upstox_creds, load_strategy_params, load_strangle_params,
                     load_session_defaults, StrategyParams, StrangleParams, StrangleTimeframe)
from upstox_client import UpstoxClient
from pnl_format import print_period_line, print_skip_line, print_summary

import data_fetcher
from backtest_engine import run_day, run_swing_trade

import strangle_data_fetcher as sdf
from strike_selector import HedgedDeltaStrangleSelector
from strangle_strategy import StrangleState
from adjustment_engine import AdjustmentEngine, AdjustmentRules, AdjustmentAction

import live_data_source


@dataclass
class RunConfig:
    strategy: str              # "iron_fly" | "strangle"
    mode: str                  # "backtest" | "forward_test" | "live"
    timeframe: str              # "intraday" | "weekly" | "monthly"
    date_from: Optional[str] = None   # None -> auto (fixed BACKTEST_WINDOW_DAYS window)
    date_to: Optional[str] = None
    capital: Optional[float] = None   # None -> auto (fixed ₹250,000)
    dry_run: bool = True
    out_dir: Optional[str] = None     # None -> auto ("output")
    use_cache: bool = True


def _resolve_defaults(config: RunConfig) -> RunConfig:
    """Fills in the auto fields (#3/#4/#5) -- called once at the top of
    run_session so every downstream function just sees concrete values."""
    sd = load_session_defaults()
    out_dir = config.out_dir or sd.output_dir
    capital = config.capital or sd.fixed_capital
    date_from, date_to = config.date_from, config.date_to
    if config.mode == "backtest" and (date_from is None or date_to is None):
        today = date.today()
        date_from = (today - timedelta(days=sd.backtest_window_days)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")
        print(f"[CONFIG] Backtest window auto-set to fixed {sd.backtest_window_days}-day "
              f"lookback: {date_from} .. {date_to} (change BACKTEST_WINDOW_DAYS in .env)")
    return replace(config, out_dir=out_dir, capital=capital, date_from=date_from, date_to=date_to)


def run_session(config: RunConfig):
    config = _resolve_defaults(config)
    os.makedirs(config.out_dir, exist_ok=True)

    if config.strategy == "iron_fly":
        _run_iron_fly_session(config)
    elif config.strategy == "strangle":
        _run_strangle_session(config)
    else:
        raise ValueError(f"Unknown strategy '{config.strategy}'")


# ---------------------------------------------------------------------------
# Shared date helpers
# ---------------------------------------------------------------------------

def _daterange(start: datetime, end: datetime):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _wednesdays_in_range(start: datetime, end: datetime):
    d = start
    while d.weekday() != 2:
        d += timedelta(days=1)
    while d <= end:
        yield d
        d += timedelta(days=7)


def _monthly_cycles(start: datetime, end: datetime, get_monthly_expiry_dt, exit_days_before_expiry: int):
    """Yields non-overlapping (entry_date, exit_date) Wednesday-start cycles.
    get_monthly_expiry_dt(entry_date) -> datetime | None (None = can't
    resolve, e.g. near the edge of Upstox's data window -> skip forward)."""
    d = start
    while d.weekday() != 2:
        d += timedelta(days=1)
    while d <= end:
        monthly_expiry = get_monthly_expiry_dt(d)
        if monthly_expiry is None:
            d += timedelta(days=7)
            continue
        exit_date = monthly_expiry - timedelta(days=exit_days_before_expiry)
        while exit_date.weekday() >= 5:
            exit_date -= timedelta(days=1)
        if exit_date <= d + timedelta(days=2):
            d += timedelta(days=7)
            continue
        yield d, exit_date
        d = exit_date + timedelta(days=1)
        while d.weekday() != 2:
            d += timedelta(days=1)


# ---------------------------------------------------------------------------
# IRON FLY / ATM IRON CONDOR
# ---------------------------------------------------------------------------

def _run_iron_fly_session(config: RunConfig):
    creds = load_upstox_creds()
    params = load_strategy_params()
    client = UpstoxClient(creds)

    if config.mode in ("forward_test", "live"):
        _run_iron_fly_live(config, params, client)
        return

    # ---- BACKTEST (real historical data, data_fetcher.py unchanged) ----
    fetch_day = data_fetcher.fetch_day_data
    fetch_swing = data_fetcher.fetch_swing_trade_data
    if config.use_cache:
        import data_cache
        fetch_day = data_cache.cached_fetch_day_data
        fetch_swing = data_cache.cached_fetch_swing_trade_data

    start = datetime.strptime(config.date_from, "%Y-%m-%d")
    end = datetime.strptime(config.date_to, "%Y-%m-%d")

    results, leg_records, period_word = [], [], "days"

    if config.timeframe == "intraday":
        for d in _daterange(start, end):
            try:
                df = fetch_day(client, params, d)
                qty = df.attrs.get("lot_size", params.lot_size) * params.lots
                result = run_day(df, params, d, quantity_override=qty)
            except Exception as e:
                print_skip_line(str(d.date()), str(e))
                continue
            _collect_ironfly_result(result, results, leg_records, str(d.date()))

    elif config.timeframe == "weekly":
        period_word = "weeks"
        for entry_date in _wednesdays_in_range(start, end):
            exit_date = entry_date + timedelta(days=5)
            label = f"{entry_date.date()}->{exit_date.date()}"
            try:
                df = fetch_swing(client, params, entry_date, exit_date)
                qty = df.attrs.get("lot_size", params.lot_size) * params.lots
                result = run_swing_trade(df, params, df.attrs["entry_dt"], df.attrs["exit_dt"],
                                          quantity_override=qty)
            except Exception as e:
                print_skip_line(label, str(e))
                continue
            _collect_ironfly_result(result, results, leg_records, label, entry_key="entry_date")

    elif config.timeframe == "monthly":
        # FIX #6: genuinely different from weekly now -- non-overlapping
        # cycles ending IRON_CONDOR_MONTHLY_EXIT_DAYS_BEFORE_EXPIRY days
        # before that cycle's real monthly expiry, with its own (wider)
        # SL/TP thresholds.
        period_word = "monthly cycles"
        monthly_params = replace(params, strategy_sl_pct=params.resolved_monthly_sl_pct(),
                                  strategy_tp_pct=params.resolved_monthly_tp_pct())

        def get_monthly_expiry(entry_date):
            try:
                exp = data_fetcher.resolve_expiries(client, params.underlying, entry_date)
                return datetime.strptime(exp["monthly_expiry_date"], "%Y-%m-%d")
            except Exception:
                return None

        for entry_date, exit_date in _monthly_cycles(start, end, get_monthly_expiry,
                                                       params.monthly_exit_days_before_expiry):
            label = f"{entry_date.date()}->{exit_date.date()} (monthly)"
            try:
                df = fetch_swing(client, monthly_params, entry_date, exit_date)
                qty = df.attrs.get("lot_size", monthly_params.lot_size) * monthly_params.lots
                result = run_swing_trade(df, monthly_params, df.attrs["entry_dt"], df.attrs["exit_dt"],
                                          quantity_override=qty)
            except Exception as e:
                print_skip_line(label, str(e))
                continue
            _collect_ironfly_result(result, results, leg_records, label, entry_key="entry_date")
    else:
        raise ValueError(f"Unknown timeframe '{config.timeframe}'")

    _summarize_and_write(results, leg_records, config, title="IRON CONDOR / IRON FLY BACKTEST",
                          period_word=period_word)


def _collect_ironfly_result(result: dict, results: list, leg_records: list, label: str,
                             entry_key: str = "date"):
    if result.get("status") != "OK":
        print_skip_line(label, result.get("status", "UNKNOWN"), tag=result.get("status", "SKIPPED"))
        return
    results.append({
        "period": result.get(entry_key, label),
        "net_credit": result["net_credit"],
        "close_reason": result["close_reason"],
        "total_pnl": result["total_pnl"],
    })
    leg_records.extend(result["legs"])
    print_period_line(label, result["total_pnl"], result["close_reason"])


def _run_iron_fly_live(config: RunConfig, params: StrategyParams, client: UpstoxClient):
    """FIX #8: forward_test now runs THIS -- real live quotes, real strike/
    expiry resolution, orders always simulated. live mode runs the identical
    path with dry_run controlling whether orders are simulated or real."""
    from strategy import IronFlyState

    paper = (config.mode == "forward_test") or config.dry_run
    label = "FORWARD-TEST (paper, live market data)" if config.mode == "forward_test" \
        else ("LIVE — DRY RUN (paper)" if paper else "LIVE — REAL ORDERS")
    print(f"\n[IRON FLY / {config.timeframe.upper()}] {label}")

    try:
        snap = live_data_source.resolve_live_ironfly_legs(client, params)
    except Exception as e:
        print(f"[ERROR] Could not resolve live legs: {e}")
        return

    qty = snap["lot_size"] * params.lots
    state = IronFlyState(
        quantity=qty, per_leg_sl_pct=params.per_leg_sl_pct,
        strategy_sl_pct=params.strategy_sl_pct, strategy_tp_pct=params.strategy_tp_pct,
        hedge_leg_sl_pct=params.hedge_leg_sl_pct, sl_mode=params.sl_mode,
        trail_activate_pct=params.trail_activate_pct, trail_giveback_pct=params.trail_giveback_pct,
    )
    entry_prices = {name: leg["ltp"] for name, leg in snap["legs"].items()}
    state.enter(entry_prices, snap["ts"])
    print(f"[ENTRY] net_credit={state.net_credit:.2f}  "
          f"legs={ {n: leg['strike'] for n, leg in snap['legs'].items()} }")
    if not paper:
        print("[LIVE ORDER] place_order() calls go here once you've watched "
              "paper behavior and are deliberately ready -- intentionally not "
              "auto-wired to real order placement in this build.")

    exit_h, exit_m = map(int, params.exit_time.split(":"))
    print(f"[MONITOR] Polling live LTP until {params.exit_time} or an SL/TP hit. "
          f"Ctrl+C to stop early (position stays open on the broker).")
    import time as _time
    while not state.closed:
        now = datetime.now()
        if now.time() >= dtime(exit_h, exit_m):
            marks = _refetch_marks(client, snap["legs"])
            state.on_tick(now, marks, force_exit=True, exit_reason="TIME_EXIT")
            break
        try:
            marks = _refetch_marks(client, snap["legs"])
        except Exception as e:
            print(f"[WARN] live quote refresh failed, retrying: {e}")
            _time.sleep(5)
            continue
        state.on_tick(now, marks)
        _time.sleep(15)

    print(f"[EXIT] reason={state.close_reason}  total_pnl={state.total_realized_pnl():.2f}")


def _refetch_marks(client: UpstoxClient, legs: dict) -> dict:
    keys = [leg["instrument_key"] for leg in legs.values()]
    ltp_map = client.get_live_ltp(keys)
    return {name: float(ltp_map.get(leg["instrument_key"], 0.0)) for name, leg in legs.items()}


# ---------------------------------------------------------------------------
# HEDGED 25-DELTA STRANGLE
# ---------------------------------------------------------------------------

def _run_strangle_session(config: RunConfig):
    creds = load_upstox_creds()
    tf_map = {"intraday": StrangleTimeframe.INTRADAY, "weekly": StrangleTimeframe.WEEKLY,
              "monthly": StrangleTimeframe.MONTHLY}
    params = load_strangle_params()
    params = replace(params, timeframe=tf_map[config.timeframe], capital_deployed=config.capital)
    client = UpstoxClient(creds)

    if config.mode in ("forward_test", "live"):
        _run_strangle_live(config, params, client)
        return

    start = datetime.strptime(config.date_from, "%Y-%m-%d")
    end = datetime.strptime(config.date_to, "%Y-%m-%d")

    if config.timeframe == "monthly":
        def get_monthly_expiry(entry_date):
            try:
                short_exp, _ = sdf._resolve_expiries(client, params.underlying, entry_date)
                return datetime.strptime(short_exp, "%Y-%m-%d")
            except Exception:
                return None
        entries = list(_monthly_cycles(start, end, get_monthly_expiry, 14))
    elif config.timeframe == "weekly":
        entries = [(d, d + timedelta(days=5)) for d in _wednesdays_in_range(start, end)]
    else:
        entries = [(d, d + timedelta(hours=6)) for d in _daterange(start, end)]

    results, leg_records = [], []
    for entry_dt_raw, exit_dt_raw in entries:
        label = f"{entry_dt_raw.date()}->{exit_dt_raw.date()}"
        try:
            result = _replay_strangle_cycle(client, params, entry_dt_raw, exit_dt_raw)
        except Exception as e:
            print_skip_line(label, str(e))
            continue
        if result is None:
            continue
        results.append(result["summary"])
        leg_records.extend(result["legs"])
        print_period_line(label, result["summary"]["total_pnl"], result["summary"]["close_reason"])

    _summarize_and_write(results, leg_records, config, title="HEDGED 25-DELTA STRANGLE BACKTEST",
                          period_word=config.timeframe + " cycles")


def _replay_strangle_cycle(client: UpstoxClient, params: StrangleParams,
                            entry_date: datetime, planned_exit_date: datetime) -> Optional[dict]:
    eh, em = map(int, params.entry_time.split(":"))
    xh, xm = map(int, params.exit_time.split(":"))
    entry_dt = entry_date.replace(hour=eh, minute=em)

    selector = HedgedDeltaStrangleSelector(
        short_leg_target_delta=params.short_leg_target_delta,
        delta_tolerance=params.delta_tolerance, hedge_target_delta=params.hedge_target_delta,
        fallback_points=params.hedge_fallback_points, r=params.risk_free_rate,
    )

    short_chain, hedge_chain = sdf.fetch_chain_at_datetime(
        client, params.underlying, params.strike_step, entry_dt, strike_window=params.strike_window)
    short_expiry, hedge_expiry = short_chain.expiry_date, hedge_chain.expiry_date

    ce_short, pe_short = selector.select_short_legs(short_chain)
    ce_hedge, pe_hedge = selector.select_hedge_legs(hedge_chain, ce_short, pe_short)

    state = StrangleState(
        quantity=params.quantity, strategy_sl_capital_pct=params.sl_capital_pct,
        strategy_tp_capital_pct=params.tp_capital_pct, capital_deployed=params.capital_deployed,
        sl_mode=params.sl_mode, trail_activate_pct=params.trail_activate_pct,
        trail_giveback_pct=params.trail_giveback_pct,
    )
    state.enter({"ce_short": ce_short.premium, "pe_short": pe_short.premium,
                 "ce_hedge": ce_hedge.premium, "pe_hedge": pe_hedge.premium}, entry_dt)
    for name, sel in (("ce_short", ce_short), ("pe_short", pe_short),
                      ("ce_hedge", ce_hedge), ("pe_hedge", pe_hedge)):
        state.fills[name][0].strike = sel.strike

    adj_engine = None
    if params.adjustment_enabled:
        rules = AdjustmentRules(delta_breach_threshold=params.adjustment_delta_breach,
                                 no_new_roll_inside_dte=params.adjustment_no_roll_inside_dte,
                                 max_adjustments_per_trade=params.adjustment_max_per_trade,
                                 max_debit_pct_capital=params.adjustment_max_debit_pct_capital)
        adj_engine = AdjustmentEngine(rules, selector, params.capital_deployed, r=params.risk_free_rate)

    exit_dt = planned_exit_date.replace(hour=xh, minute=xm) if params.timeframe != StrangleTimeframe.INTRADAY \
        else entry_date.replace(hour=xh, minute=xm)

    cur = entry_dt + timedelta(minutes=params.replay_interval_mins)
    while cur <= exit_dt and not state.closed:
        try:
            sc, hc = sdf.fetch_chain_at_datetime(client, params.underlying, params.strike_step, cur,
                                                  short_expiry=short_expiry, hedge_expiry=hedge_expiry,
                                                  strike_window=params.strike_window)
        except Exception:
            cur += timedelta(minutes=params.replay_interval_mins)
            continue

        strikes = state.current_strikes()
        marks = {}
        for name in ("ce_short", "pe_short"):
            row = next((r for r in sc.rows if r.strike == strikes[name]), None)
            marks[name] = (row.ce_premium if "ce" in name else row.pe_premium) if row else None
        for name in ("ce_hedge", "pe_hedge"):
            row = next((r for r in hc.rows if r.strike == strikes[name]), None)
            marks[name] = (row.ce_premium if "ce" in name else row.pe_premium) if row else None
        if any(v is None for v in marks.values()):
            cur += timedelta(minutes=params.replay_interval_mins)
            continue

        force = cur >= exit_dt
        state.on_tick(cur, marks, force_exit=force, exit_reason="TIME_EXIT" if force else None)

        if adj_engine is not None and not state.closed:
            dte = (datetime.strptime(short_expiry, "%Y-%m-%d") - cur).total_seconds() / 86400.0
            trig = adj_engine.check_triggers(state, sc, strikes["ce_short"], strikes["pe_short"], dte)
            action = adj_engine.next_action(state, trig)
            if action == AdjustmentAction.REDUCE_OR_CLOSE:
                for name in state.open_legs():
                    state.close_leg(name, marks[name], "ADJUSTMENT_REDUCE_CLOSE", cur)
                state.closed = True
                state.close_reason = "ADJUSTMENT_REDUCE_CLOSE"
            elif action != AdjustmentAction.NONE:
                tested = "ce" if trig.ce_breached else "pe"
                outcome = adj_engine.execute_adjustment(action, state, sc, hc, tested, cur)
                if outcome.get("status") == "ROLL":
                    state.roll_leg(outcome["short_leg_name"], marks[outcome["short_leg_name"]],
                                    outcome["short"].strike, outcome["short"].premium, action.value, cur)
                    state.roll_leg(outcome["hedge_leg_name"], marks[outcome["hedge_leg_name"]],
                                    outcome["hedge"].strike, outcome["hedge"].premium, action.value, cur)

        cur += timedelta(minutes=params.replay_interval_mins)

    if not state.closed:
        strikes = state.current_strikes()
        marks = {}
        ok = True
        for name in ("ce_short", "pe_short"):
            row = next((r for r in short_chain.rows if r.strike == strikes[name]), None)
            if row is None:
                ok = False
                break
            marks[name] = row.ce_premium if "ce" in name else row.pe_premium
        for name in ("ce_hedge", "pe_hedge"):
            row = next((r for r in hedge_chain.rows if r.strike == strikes[name]), None)
            if row is None:
                ok = False
                break
            marks[name] = row.ce_premium if "ce" in name else row.pe_premium
        if ok:
            state.on_tick(exit_dt, marks, force_exit=True, exit_reason="DATA_END")
        else:
            return None

    leg_rows = []
    for name in state.fills:
        for f in state.fills[name]:
            leg_rows.append({
                "entry_date": entry_dt.date().isoformat(), "leg": name, "strike": f.strike,
                "side": f.side.value, "qty": f.quantity, "entry_price": f.entry_price,
                "exit_price": f.exit_price, "exit_reason": f.exit_reason,
                "leg_pnl": f.realized_pnl(),
            })

    return {
        "summary": {"period": entry_dt.date().isoformat(), "net_credit": state.net_credit,
                    "close_reason": state.close_reason, "total_pnl": state.total_realized_pnl(),
                    "adjustments": state.adjustment_count},
        "legs": leg_rows,
    }


def _run_strangle_live(config: RunConfig, params: StrangleParams, client: UpstoxClient):
    """FIX #8: forward_test runs this too now (paper=True forced), against
    REAL live option-chain quotes via strangle_data_fetcher.fetch_live_chain
    -- not a historical replay."""
    paper = (config.mode == "forward_test") or config.dry_run
    label = "FORWARD-TEST (paper, live market data)" if config.mode == "forward_test" \
        else ("LIVE — DRY RUN (paper)" if paper else "LIVE — REAL ORDERS")
    print(f"\n[STRANGLE / {config.timeframe.upper()}] {label}")

    selector = HedgedDeltaStrangleSelector(
        short_leg_target_delta=params.short_leg_target_delta, delta_tolerance=params.delta_tolerance,
        hedge_target_delta=params.hedge_target_delta, fallback_points=params.hedge_fallback_points,
        r=params.risk_free_rate,
    )

    try:
        short_expiry, hedge_expiry = sdf.resolve_live_expiries(client, params.underlying, datetime.now())
        short_chain, hedge_chain = sdf.fetch_live_chain(client, params.underlying, params.strike_step,
                                                          short_expiry, hedge_expiry,
                                                          strike_window=params.strike_window)
        ce_short, pe_short = selector.select_short_legs(short_chain)
        ce_hedge, pe_hedge = selector.select_hedge_legs(hedge_chain, ce_short, pe_short)
    except Exception as e:
        print(f"[ERROR] Could not resolve live strangle legs: {e}")
        return

    state = StrangleState(
        quantity=params.quantity, strategy_sl_capital_pct=params.sl_capital_pct,
        strategy_tp_capital_pct=params.tp_capital_pct, capital_deployed=params.capital_deployed,
        sl_mode=params.sl_mode, trail_activate_pct=params.trail_activate_pct,
        trail_giveback_pct=params.trail_giveback_pct,
    )
    now = datetime.now()
    state.enter({"ce_short": ce_short.premium, "pe_short": pe_short.premium,
                 "ce_hedge": ce_hedge.premium, "pe_hedge": pe_hedge.premium}, now)
    for name, sel in (("ce_short", ce_short), ("pe_short", pe_short),
                      ("ce_hedge", ce_hedge), ("pe_hedge", pe_hedge)):
        state.fills[name][0].strike = sel.strike

    print(f"[ENTRY] net_credit={state.net_credit:.2f}  "
          f"strikes={ {n: f[0].strike for n, f in state.fills.items()} }")
    if not paper:
        print("[LIVE ORDER] place_order() calls go here once ready -- not auto-wired in this build.")

    xh, xm = map(int, params.exit_time.split(":"))
    import time as _time
    while not state.closed:
        n = datetime.now()
        if n.time() >= dtime(xh, xm):
            sc, hc = sdf.fetch_live_chain(client, params.underlying, params.strike_step,
                                           short_expiry, hedge_expiry, strike_window=params.strike_window)
            marks = _live_marks(state, sc, hc)
            state.on_tick(n, marks, force_exit=True, exit_reason="TIME_EXIT")
            break
        try:
            sc, hc = sdf.fetch_live_chain(client, params.underlying, params.strike_step,
                                           short_expiry, hedge_expiry, strike_window=params.strike_window)
            marks = _live_marks(state, sc, hc)
        except Exception as e:
            print(f"[WARN] live chain refresh failed, retrying: {e}")
            _time.sleep(10)
            continue
        state.on_tick(n, marks)
        _time.sleep(30)

    print(f"[EXIT] reason={state.close_reason}  total_pnl={state.total_realized_pnl():.2f}")


def _live_marks(state: StrangleState, short_chain, hedge_chain) -> dict:
    strikes = state.current_strikes()
    marks = {}
    for name in ("ce_short", "pe_short"):
        row = next((r for r in short_chain.rows if r.strike == strikes[name]), None)
        marks[name] = (row.ce_premium if "ce" in name else row.pe_premium) if row else 0.0
    for name in ("ce_hedge", "pe_hedge"):
        row = next((r for r in hedge_chain.rows if r.strike == strikes[name]), None)
        marks[name] = (row.ce_premium if "ce" in name else row.pe_premium) if row else 0.0
    return marks


# ---------------------------------------------------------------------------
# Shared summary/output
# ---------------------------------------------------------------------------

def _summarize_and_write(results: list, leg_records: list, config: RunConfig,
                          title: str, period_word: str):
    if not results:
        print("No periods produced results. Nothing to summarize.")
        return
    df = pd.DataFrame(results)
    legs_df = pd.DataFrame(leg_records)

    out_csv = os.path.join(config.out_dir, f"{config.strategy}_{config.timeframe}_trades.csv")
    df.to_csv(out_csv, index=False)
    legs_df.to_csv(out_csv.replace(".csv", "_legs.csv"), index=False)

    total_pnl = df["total_pnl"].sum()
    win = (df["total_pnl"] > 0).sum()
    loss = (df["total_pnl"] <= 0).sum()
    win_rate = win / len(df) * 100
    equity = df["total_pnl"].cumsum()
    max_dd = (equity - equity.cummax()).min()

    print_summary(title=title, rows=df, total_pnl=total_pnl, max_dd=max_dd, win_rate=win_rate,
                  avg_pnl=df["total_pnl"].mean(), n_win=win, n_loss=loss, capital=config.capital,
                  close_reason_counts=df["close_reason"].value_counts(), period_word=period_word)
    print(f"\nDetailed logs written to {out_csv} and {out_csv.replace('.csv', '_legs.csv')}")
