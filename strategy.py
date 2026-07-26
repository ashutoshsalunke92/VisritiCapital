"""
Broker-agnostic Iron Fly / ATM Iron Condor position state.

CHANGE IN THIS REVISION:
  SL/TP decision logic has been moved OUT of on_tick() and into the
  TradeExitEngine (exit_engine/). IronFlyState now owns only:
    - Leg tracking (entry/exit prices, open/closed state)
    - P&L accounting (unrealized, realized, net credit)
    - The raw on_tick() call that returns marks to the engine caller

  The exit engine is NOT called from inside IronFlyState — it is called
  by backtest_engine.py / trading_engine.py (the tick drivers) which then
  tell the state what to do. This keeps IronFlyState broker/engine-agnostic
  and makes it safe to test in isolation.

  The legacy trailing SL fields (sl_mode, trail_activate_pct, etc.) are
  retained as dataclass fields so old code that constructs IronFlyState
  with those kwargs doesn't break — but the actual trailing logic is now
  executed by exit_engine/policies.py.TradeLevelExitPolicy.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Leg:
    name: str
    side: Side
    quantity: int
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    is_open: bool = False

    def sl_price(self, sl_pct: float) -> Optional[float]:
        if self.entry_price is None:
            return None
        if self.side == Side.SELL:
            return self.entry_price * (1 + sl_pct)
        else:
            return self.entry_price * (1 - sl_pct)

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.entry_price is None:
            return 0.0
        if self.side == Side.SELL:
            return (self.entry_price - mark_price) * self.quantity
        else:
            return (mark_price - self.entry_price) * self.quantity

    def realized_pnl(self) -> float:
        if self.entry_price is None or self.exit_price is None:
            return 0.0
        if self.side == Side.SELL:
            return (self.entry_price - self.exit_price) * self.quantity
        else:
            return (self.exit_price - self.entry_price) * self.quantity


LEG_SPECS = [
    ("ce_atm", Side.SELL),
    ("ce_otm7", Side.BUY),
    ("pe_atm", Side.SELL),
    ("pe_otm7", Side.BUY),
]


@dataclass
class IronFlyState:
    quantity: int
    per_leg_sl_pct: float
    strategy_sl_pct: float
    strategy_tp_pct: float
    hedge_leg_sl_pct: Optional[float] = None
    # Legacy trailing SL fields — retained for backward compatibility only.
    # Actual trailing logic now lives in exit_engine/policies.py.
    sl_mode: str = "STATIC"
    trail_activate_pct: float = 0.5
    trail_giveback_pct: float = 0.3
    trailing_armed: bool = False
    peak_pnl: float = 0.0
    legs: dict = field(default_factory=dict)
    net_credit: Optional[float] = None
    entered: bool = False
    closed: bool = False
    close_reason: Optional[str] = None
    events: list = field(default_factory=list)

    def enter(self, prices: dict, ts):
        """prices: {'ce_atm': x, 'pe_atm': x, 'ce_otm7': x, 'pe_otm7': x}"""
        self.legs = {name: Leg(name=name, side=side, quantity=self.quantity)
                     for name, side in LEG_SPECS}
        for name, leg in self.legs.items():
            leg.entry_price = prices[name]
            leg.is_open = True
        credit = (
            self.legs["ce_atm"].entry_price + self.legs["pe_atm"].entry_price
            - self.legs["ce_otm7"].entry_price - self.legs["pe_otm7"].entry_price
        ) * self.quantity
        self.net_credit = credit
        self.entered = True
        self._log(ts, "ENTRY", f"Net credit = {credit:.2f}")

    def _log(self, ts, kind, msg):
        self.events.append({"ts": ts, "type": kind, "msg": msg})

    def open_legs(self):
        return [leg for leg in self.legs.values() if leg.is_open]

    def combined_unrealized_pnl(self, prices: dict) -> float:
        total = 0.0
        for name, leg in self.legs.items():
            if leg.is_open:
                total += leg.unrealized_pnl(prices[name])
            else:
                total += leg.realized_pnl()
        return total

    def close_leg(self, name: str, price: float, reason: str, ts):
        """Close one leg — called by the tick driver after the exit engine
        returns a LegDecision."""
        leg = self.legs.get(name)
        if leg is None or not leg.is_open:
            return
        leg.exit_price = price
        leg.exit_reason = reason
        leg.is_open = False
        self._log(ts, "EXIT", f"{name} closed @ {price:.2f} ({reason})")

    def close_all(self, prices: dict, reason: str, ts):
        """Close every open leg — called by the tick driver after the exit
        engine returns close_entire_trade=True."""
        for leg in self.open_legs():
            self.close_leg(leg.name, prices[leg.name], reason, ts)
        self.closed = True
        self.close_reason = reason

    def on_tick(self, ts, prices: dict, force_exit: bool = False,
                exit_reason: str = "TIME_EXIT"):
        """Minimal tick method retained for backward compatibility with
        run_backtest.py / run_swing_backtest.py direct calls (those scripts
        don't use the exit engine yet and call this directly).

        When called via trading_engine.py / backtest_engine.py with the exit
        engine wired in, the tick driver calls the exit engine FIRST and then
        calls close_all() / close_leg() on this object — on_tick() is NOT
        called in that path.

        This method applies the original hardcoded rules so the legacy CLI
        scripts (run_backtest.py, run_swing_backtest.py) keep working without
        any changes.
        """
        if not self.entered or self.closed:
            return []

        closed_now = []

        if force_exit:
            for leg in self.open_legs():
                self.close_leg(leg.name, prices[leg.name], exit_reason, ts)
                closed_now.append(leg.name)
            self.closed = True
            self.close_reason = exit_reason
            return closed_now

        # Per-leg SL (short legs only; hedge legs only if HEDGE_LEG_SL_PCT set)
        for leg in self.open_legs():
            if leg.side == Side.BUY:
                if self.hedge_leg_sl_pct is None:
                    continue
                sl = leg.sl_price(self.hedge_leg_sl_pct)
                reason = "HEDGE_LEG_SL"
            else:
                sl = leg.sl_price(self.per_leg_sl_pct)
                reason = "PER_LEG_SL"
            mark = prices[leg.name]
            triggered = (leg.side == Side.SELL and mark >= sl) or \
                        (leg.side == Side.BUY and mark <= sl)
            if triggered:
                self.close_leg(leg.name, mark, reason, ts)
                closed_now.append(leg.name)

        if not self.open_legs():
            self.closed = True
            self.close_reason = "ALL_LEGS_SL"
            return closed_now

        pnl = self.combined_unrealized_pnl(prices)
        if self.net_credit and self.net_credit != 0:
            sl_threshold = -abs(self.net_credit) * self.strategy_sl_pct
            tp_threshold = abs(self.net_credit) * self.strategy_tp_pct

            if pnl <= sl_threshold:
                for leg in self.open_legs():
                    self.close_leg(leg.name, prices[leg.name], "STRATEGY_SL", ts)
                    closed_now.append(leg.name)
                self.closed = True
                self.close_reason = "STRATEGY_SL"
                return closed_now

            if self.sl_mode == "TRAILING":
                if not self.trailing_armed and pnl >= tp_threshold * self.trail_activate_pct:
                    self.trailing_armed = True
                    self.peak_pnl = pnl
                    self._log(ts, "TRAIL_ARM", f"Trailing SL armed at pnl={pnl:.2f}")
                if self.trailing_armed:
                    self.peak_pnl = max(self.peak_pnl, pnl)
                    trail_stop = self.peak_pnl * (1 - self.trail_giveback_pct)
                    if pnl <= trail_stop:
                        for leg in self.open_legs():
                            self.close_leg(leg.name, prices[leg.name], "TRAILING_SL", ts)
                            closed_now.append(leg.name)
                        self.closed = True
                        self.close_reason = "TRAILING_SL"
            else:
                if pnl >= tp_threshold:
                    for leg in self.open_legs():
                        self.close_leg(leg.name, prices[leg.name], "STRATEGY_TP", ts)
                        closed_now.append(leg.name)
                    self.closed = True
                    self.close_reason = "STRATEGY_TP"

        return closed_now

    def total_realized_pnl(self) -> float:
        return sum(leg.realized_pnl() for leg in self.legs.values())
