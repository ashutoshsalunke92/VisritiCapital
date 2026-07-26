"""
Broker-agnostic Short Strangle Hedged position state.

CHANGE IN THIS REVISION:
  SL/TP decision logic delegated to exit_engine/ (same as strategy.py).
  StrangleState now owns only leg tracking, P&L accounting, and roll
  bookkeeping. The on_tick() method is retained for backward compat but
  the primary tick path (trading_engine.py) uses the exit engine instead.

  current_strikes() is still called every tick by the adjustment engine to
  get live open strikes — this is unchanged and correct.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class LegFill:
    strike: float
    side: Side
    quantity: int
    entry_price: float
    entry_ts: object
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_ts: object = None
    is_open: bool = True

    def realized_pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        if self.side == Side.SELL:
            return (self.entry_price - self.exit_price) * self.quantity
        return (self.exit_price - self.entry_price) * self.quantity

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.side == Side.SELL:
            return (self.entry_price - mark_price) * self.quantity
        else:
            return (mark_price - self.entry_price) * self.quantity


LEG_NAMES = ["ce_short", "pe_short", "ce_hedge", "pe_hedge"]
_SIDE_FOR = {
    "ce_short": Side.SELL, "pe_short": Side.SELL,
    "ce_hedge": Side.BUY,  "pe_hedge": Side.BUY,
}


@dataclass
class StrangleState:
    quantity: int
    strategy_sl_capital_pct: float
    strategy_tp_capital_pct: float
    capital_deployed: float
    # Legacy trailing SL fields — retained for backward compatibility only.
    # Actual trailing logic now lives in exit_engine/policies.py.
    sl_mode: str = "STATIC"
    trail_activate_pct: float = 0.5
    trail_giveback_pct: float = 0.3
    trailing_armed: bool = False
    peak_pnl: float = 0.0

    entered: bool = False
    closed: bool = False
    close_reason: Optional[str] = None
    net_credit: Optional[float] = None
    fills: dict = field(default_factory=lambda: {n: [] for n in LEG_NAMES})
    adjustment_count: int = 0
    events: list = field(default_factory=list)

    def _log(self, ts, kind, msg):
        self.events.append({"ts": ts, "type": kind, "msg": msg})

    def enter(self, legs: dict, ts):
        """legs: {'ce_short': premium, 'pe_short': premium, ...}"""
        for name in LEG_NAMES:
            premium = legs[name]
            fill = LegFill(
                strike=None, side=_SIDE_FOR[name], quantity=self.quantity,
                entry_price=premium, entry_ts=ts,
            )
            self.fills[name] = [fill]
        credit = (
            self.fills["ce_short"][0].entry_price + self.fills["pe_short"][0].entry_price
            - self.fills["ce_hedge"][0].entry_price - self.fills["pe_hedge"][0].entry_price
        ) * self.quantity
        self.net_credit = credit
        self.entered = True
        self._log(ts, "ENTRY", f"Net credit = {credit:.2f}")

    def open_leg_fill(self, name: str, strike: float, premium: float, ts):
        fill = LegFill(
            strike=strike, side=_SIDE_FOR[name], quantity=self.quantity,
            entry_price=premium, entry_ts=ts,
        )
        self.fills[name].append(fill)
        self._log(ts, "OPEN_LEG", f"{name} opened @ strike={strike} premium={premium:.2f}")

    def _open_fill(self, name: str) -> Optional[LegFill]:
        for f in reversed(self.fills[name]):
            if f.is_open:
                return f
        return None

    def current_strikes(self) -> dict:
        """Returns the LIVE open strike per leg. Called every tick by the
        adjustment engine so it always watches the current strike, not the
        stale entry strike (which changes after a roll)."""
        return {name: (self._open_fill(name).strike if self._open_fill(name) else None)
                for name in LEG_NAMES}

    def close_leg(self, name: str, price: float, reason: str, ts):
        """Close one leg — called by the tick driver after the exit engine
        returns a LegDecision, or by force_exit logic."""
        f = self._open_fill(name)
        if f is None:
            return
        f.exit_price = price
        f.exit_reason = reason
        f.exit_ts = ts
        f.is_open = False
        self._log(ts, "EXIT_LEG", f"{name} closed @ {price:.2f} ({reason})")

    def close_all(self, marks: dict, reason: str, ts):
        """Close every open leg — called after the exit engine returns
        close_entire_trade=True."""
        for name in self.open_legs():
            self.close_leg(name, marks[name], reason, ts)
        self.closed = True
        self.close_reason = reason

    def roll_leg(self, name: str, close_price: float, new_strike: float,
                 new_premium: float, reason: str, ts):
        """Close the current fill and open a replacement at a new strike.
        Called by the adjustment engine via trading_engine.py."""
        self.close_leg(name, close_price, reason, ts)
        self.open_leg_fill(name, new_strike, new_premium, ts)
        self.adjustment_count += 1

    def open_legs(self) -> list:
        return [name for name in LEG_NAMES if self._open_fill(name) is not None]

    def combined_unrealized_pnl(self, marks: dict) -> float:
        total = 0.0
        for name in LEG_NAMES:
            for f in self.fills[name]:
                if f.is_open:
                    total += f.unrealized_pnl(marks[name])
                else:
                    total += f.realized_pnl()
        return total

    def total_realized_pnl(self) -> float:
        return sum(f.realized_pnl() for fills in self.fills.values() for f in fills)

    def on_tick(self, ts, marks: dict, force_exit: bool = False,
                exit_reason: str = "TIME_EXIT"):
        """Retained for backward compat with code that calls this directly
        (e.g. the legacy CLI scripts). The primary tick path in trading_engine
        uses the exit engine instead and calls close_leg / close_all directly.
        """
        if not self.entered or self.closed:
            return []

        closed_now = []
        if force_exit:
            for name in self.open_legs():
                self.close_leg(name, marks[name], exit_reason, ts)
                closed_now.append(name)
            self.closed = True
            self.close_reason = exit_reason
            return closed_now

        pnl = self.combined_unrealized_pnl(marks)
        sl_threshold = -abs(self.capital_deployed) * self.strategy_sl_capital_pct
        tp_threshold = abs(self.capital_deployed) * self.strategy_tp_capital_pct

        if pnl <= sl_threshold:
            for name in self.open_legs():
                self.close_leg(name, marks[name], "STRATEGY_SL", ts)
                closed_now.append(name)
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
                    for name in self.open_legs():
                        self.close_leg(name, marks[name], "TRAILING_SL", ts)
                        closed_now.append(name)
                    self.closed = True
                    self.close_reason = "TRAILING_SL"
        else:
            if pnl >= tp_threshold:
                for name in self.open_legs():
                    self.close_leg(name, marks[name], "STRATEGY_TP", ts)
                    closed_now.append(name)
                self.closed = True
                self.close_reason = "STRATEGY_TP"

        return closed_now
