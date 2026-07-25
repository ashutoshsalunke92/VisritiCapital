"""
Broker-agnostic strategy logic. Knows nothing about Dhan, backtesting, or
live feeds — it just takes price ticks and tells you what to do. This is
the piece shared by backtest_engine.py (replaying historical candles) and
live_trader.py (replaying real-time ticks) so both run *identical* rules.

CHANGE FROM ORIGINAL (see TUNING_GUIDE.md for the full writeup):
  Added `hedge_leg_sl_pct`, applied separately from `per_leg_sl_pct` to the
  two long OTM7 hedge legs. Default is None, which means hedge legs are
  NEVER closed on a per-leg basis — they only close via combined strategy
  SL/TP or the final time exit, same as before.

  Why: the original code applied the same 25% per-leg SL to hedge legs as
  to the short ATM legs. Hedge legs are cheap, deep-OTM options — a 25%
  move on a ₹6 premium is ₹1.50, well inside normal noise/spread. Closing
  the hedge there strips your protection and locks a small loss on the one
  leg whose job is to sit still until it's actually needed. Set
  HEDGE_LEG_SL_PCT in .env to a number (e.g. matching PER_LEG_SL_PCT) to
  restore the exact original behavior if you want to A/B it.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Leg:
    name: str            # "ce_atm", "pe_atm", "ce_otm7", "pe_otm7"
    side: Side
    quantity: int
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    is_open: bool = False

    def sl_price(self, sl_pct: float) -> Optional[float]:
        if self.entry_price is None:
            return None
        # Adverse move for a SELL is the price rising; for a BUY it's the
        # price falling (the hedge losing value).
        if self.side == Side.SELL:
            return self.entry_price * (1 + sl_pct)
        else:
            return self.entry_price * (1 - sl_pct)

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.entry_price is None:
            return 0.0
        direction = 1 if self.side == Side.SELL else -1
        return direction * (self.entry_price - mark_price) * self.quantity \
            if self.side == Side.SELL else \
            (mark_price - self.entry_price) * self.quantity

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
    hedge_leg_sl_pct: Optional[float] = None   # NEW — see module docstring
    # --- NEW: trailing SL (opt-in via SL_MODE=TRAILING in .env, config.py) ---
    # STATIC  (default): unchanged original behavior -- fixed SL/TP thresholds
    #         off net_credit, exactly as before.
    # TRAILING: once unrealized profit reaches `trail_activate_pct` of the
    #         static TP threshold, the fixed TP is dropped and a trailing stop
    #         takes over instead -- it ratchets up behind the running peak
    #         profit and exits once profit gives back `trail_giveback_pct` of
    #         that peak. The static SL always stays live underneath as a hard
    #         floor, even after trailing arms, so a fast reversal before the
    #         trail catches up still can't lose more than the original SL.
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
        """prices: {'ce_atm': x, 'pe_atm': x, 'ce_otm7': x, 'pe_otm7': x} — fill prices."""
        self.legs = {name: Leg(name=name, side=side, quantity=self.quantity)
                     for name, side in LEG_SPECS}
        for name, leg in self.legs.items():
            leg.entry_price = prices[name]
            leg.is_open = True
        credit = (self.legs["ce_atm"].entry_price + self.legs["pe_atm"].entry_price
                  - self.legs["ce_otm7"].entry_price - self.legs["pe_otm7"].entry_price) * self.quantity
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

    def on_tick(self, ts, prices: dict, force_exit: bool = False, exit_reason: str = "TIME_EXIT"):
        """Call once per bar/tick with current mark prices for all legs still open.
        Applies, in order: per-leg SL (short legs) -> hedge-leg SL (if enabled)
        -> combined strategy SL -> combined TP -> forced time exit.
        Returns list of leg names closed on this tick."""
        if not self.entered or self.closed:
            return []

        closed_now = []

        if force_exit:
            for leg in self.open_legs():
                self._close_leg(leg, prices[leg.name], exit_reason, ts)
                closed_now.append(leg.name)
            self.closed = True
            self.close_reason = exit_reason
            return closed_now

        # 1. per-leg stop loss — short (SELL) legs always checked; hedge
        #    (BUY) legs only checked if hedge_leg_sl_pct is explicitly set.
        for leg in self.open_legs():
            if leg.side == Side.BUY:
                if self.hedge_leg_sl_pct is None:
                    continue  # hedge leg rides until combined SL/TP/time exit
                sl = leg.sl_price(self.hedge_leg_sl_pct)
                reason = "HEDGE_LEG_SL"
            else:
                sl = leg.sl_price(self.per_leg_sl_pct)
                reason = "PER_LEG_SL"
            mark = prices[leg.name]
            triggered = (leg.side == Side.SELL and mark >= sl) or \
                        (leg.side == Side.BUY and mark <= sl)
            if triggered:
                self._close_leg(leg, mark, reason, ts)
                closed_now.append(leg.name)

        if not self.open_legs():
            self.closed = True
            self.close_reason = "ALL_LEGS_SL"
            return closed_now

        # 2 & 3. combined strategy SL / TP, evaluated against net credit
        pnl = self.combined_unrealized_pnl(prices)
        if self.net_credit and self.net_credit != 0:
            sl_threshold = -abs(self.net_credit) * self.strategy_sl_pct
            tp_threshold = abs(self.net_credit) * self.strategy_tp_pct

            # Hard SL always live first, in both modes -- a trailing stop
            # never gets a chance to widen risk beyond the static floor.
            if pnl <= sl_threshold:
                for leg in self.open_legs():
                    self._close_leg(leg, prices[leg.name], "STRATEGY_SL", ts)
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
                            self._close_leg(leg, prices[leg.name], "TRAILING_SL", ts)
                            closed_now.append(leg.name)
                        self.closed = True
                        self.close_reason = "TRAILING_SL"
                # NOTE: once trailing is armed, the fixed TP no longer fires --
                # the trailing stop is what lets profit run past the old cap.
            else:
                if pnl >= tp_threshold:
                    for leg in self.open_legs():
                        self._close_leg(leg, prices[leg.name], "STRATEGY_TP", ts)
                        closed_now.append(leg.name)
                    self.closed = True
                    self.close_reason = "STRATEGY_TP"

        return closed_now

    def _close_leg(self, leg: Leg, price: float, reason: str, ts):
        leg.exit_price = price
        leg.exit_reason = reason
        leg.is_open = False
        self._log(ts, "EXIT", f"{leg.name} closed @ {price:.2f} ({reason})")

    def total_realized_pnl(self) -> float:
        return sum(leg.realized_pnl() for leg in self.legs.values())
