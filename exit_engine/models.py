"""
Universal Trade Exit Engine — domain models.

Hierarchy: Strategy -> Trade -> Position -> Leg, every object globally
unique-ID'd. The engine only ever evaluates by TradeID (evaluate_trade
receives a TradeContext addressed to exactly one trade) — no cross-trade
state sharing, no cross-strategy state sharing. This module has zero
knowledge of Iron Fly / Strangle / options in general; it's pure domain
shape, deliberately strategy-agnostic per the spec.
"""
from __future__ import annotations

import itertools
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class TradeState(Enum):
    CREATED = "CREATED"
    WAITING_ENTRY = "WAITING_ENTRY"
    ENTERED = "ENTERED"
    ACTIVE = "ACTIVE"
    ADJUSTED = "ADJUSTED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class ExitReason(Enum):
    NONE = "NONE"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    BROKER_RISK_EXIT = "BROKER_RISK_EXIT"
    MANUAL_EXIT = "MANUAL_EXIT"
    TRADE_LEVEL_SL = "TRADE_LEVEL_SL"
    TRADE_LEVEL_TP = "TRADE_LEVEL_TP"
    TRADE_LEVEL_TRAILING_SL = "TRADE_LEVEL_TRAILING_SL"
    LEG_LEVEL_SL = "LEG_LEVEL_SL"
    LEG_LEVEL_TP = "LEG_LEVEL_TP"
    LEG_LEVEL_TRAILING_SL = "LEG_LEVEL_TRAILING_SL"
    STRATEGY_EXIT_RULE = "STRATEGY_EXIT_RULE"
    TIME_EXIT = "TIME_EXIT"


class TriggerSource(Enum):
    ENTRY_PREMIUM = "ENTRY_PREMIUM"
    LIVE_PNL = "LIVE_PNL"
    MARGIN_USED = "MARGIN_USED"
    CAPITAL_ALLOCATED = "CAPITAL_ALLOCATED"
    PREMIUM = "PREMIUM"


class ExitType(Enum):
    PERCENTAGE = "PERCENTAGE"
    PREMIUM_POINTS = "PREMIUM_POINTS"
    RUPEES = "RUPEES"


class ExitAction(Enum):
    EXIT_LEG = "EXIT_LEG"
    ROLL_LEG = "ROLL_LEG"
    REPLACE_LEG = "REPLACE_LEG"
    NOTIFY_ONLY = "NOTIFY_ONLY"
    CLOSE_ENTIRE_TRADE = "CLOSE_ENTIRE_TRADE"


class ExitPriority(Enum):
    """Highest to lowest priority, per spec section 6. Lower integer =
    higher priority, so sorting ascending gives evaluation/override order."""
    EMERGENCY_EXIT = 1
    BROKER_RISK_EXIT = 2
    MANUAL_EXIT = 3
    TRADE_LEVEL_SL = 4
    TRADE_LEVEL_TP = 5
    LEG_LEVEL_SL = 6
    LEG_LEVEL_TP = 7
    STRATEGY_EXIT_RULES = 8

    @classmethod
    def for_reason(cls, reason: ExitReason) -> "ExitPriority":
        mapping = {
            ExitReason.EMERGENCY_EXIT: cls.EMERGENCY_EXIT,
            ExitReason.BROKER_RISK_EXIT: cls.BROKER_RISK_EXIT,
            ExitReason.MANUAL_EXIT: cls.MANUAL_EXIT,
            ExitReason.TRADE_LEVEL_SL: cls.TRADE_LEVEL_SL,
            ExitReason.TRADE_LEVEL_TRAILING_SL: cls.TRADE_LEVEL_SL,
            ExitReason.TRADE_LEVEL_TP: cls.TRADE_LEVEL_TP,
            ExitReason.LEG_LEVEL_SL: cls.LEG_LEVEL_SL,
            ExitReason.LEG_LEVEL_TRAILING_SL: cls.LEG_LEVEL_SL,
            ExitReason.LEG_LEVEL_TP: cls.LEG_LEVEL_TP,
            ExitReason.STRATEGY_EXIT_RULE: cls.STRATEGY_EXIT_RULES,
            ExitReason.TIME_EXIT: cls.STRATEGY_EXIT_RULES,
        }
        return mapping.get(reason, cls.STRATEGY_EXIT_RULES)


# --------------------------------------------------------------------------- #
# Hierarchy: Strategy -> Trade -> Position -> Leg
# --------------------------------------------------------------------------- #

@dataclass
class Leg:
    leg_id: str
    name: str                      # "ce_short", "pe_hedge", "ce_atm", ...
    side: str                      # "BUY" | "SELL"
    quantity: int
    entry_premium: float           # immutable once set
    current_premium: float
    is_open: bool = True
    archived: bool = False         # True once rolled/replaced -- superseded, kept for audit
    parent_leg_id: Optional[str] = None   # if this leg was created by a roll/replace
    trailing_armed: bool = False
    peak_value: float = 0.0

    def unrealized_pnl(self) -> float:
        sign = 1 if self.side == "SELL" else -1
        return sign * (self.entry_premium - self.current_premium) * self.quantity \
            if self.side == "SELL" else (self.current_premium - self.entry_premium) * self.quantity


@dataclass
class Position:
    position_id: str
    legs: dict = field(default_factory=dict)   # leg_id -> Leg (includes archived, for audit)

    def open_legs(self) -> list:
        return [l for l in self.legs.values() if l.is_open and not l.archived]


@dataclass
class Trade:
    trade_id: str
    strategy_id: str
    position: Position
    state: TradeState = TradeState.CREATED
    entry_premium: Optional[float] = None       # set exactly once, never overwritten
    realized_pnl: float = 0.0                    # never resets during adjustments
    margin_used: float = 0.0
    capital_allocated: float = 0.0
    adjustment_count: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    trailing_armed: bool = False
    peak_value: float = 0.0
    _entry_premium_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def set_entry_premium_once(self, value: float):
        with self._entry_premium_lock:
            if self.entry_premium is not None:
                return  # immutable -- silently ignore attempts to overwrite
            self.entry_premium = value

    def current_position_premium(self) -> float:
        """Sum of live premiums across open legs -- changes on adjustment,
        unlike entry_premium which is fixed forever."""
        total = 0.0
        for leg in self.position.open_legs():
            sign = 1 if leg.side == "SELL" else -1
            total += sign * leg.current_premium
        return total

    def unrealized_pnl(self) -> float:
        return sum(l.unrealized_pnl() for l in self.position.open_legs())

    def current_pnl(self) -> float:
        """realized + unrealized. Never reset by adjustments -- only reset
        (by the caller, on a fresh Trade) once the whole strategy exits."""
        return self.realized_pnl + self.unrealized_pnl()


@dataclass
class Strategy:
    strategy_id: str
    name: str
    trades: dict = field(default_factory=dict)   # trade_id -> Trade


def new_leg_id() -> str:
    return _new_id("LEG")


def new_trade_id() -> str:
    return _new_id("TRD")


def new_position_id() -> str:
    return _new_id("POS")


def new_strategy_id() -> str:
    return _new_id("STRAT")


# --------------------------------------------------------------------------- #
# Engine input/output shapes
# --------------------------------------------------------------------------- #

@dataclass
class TradeContext:
    """What the caller (backtest replay loop / live poller) hands to
    evaluate_trade() on every tick. Carries the CURRENT marks so the engine
    never has to fetch data itself -- keeps it broker/strategy agnostic."""
    trade: Trade
    leg_marks: dict                 # leg_id -> current premium (fresh this tick)
    as_of: datetime
    emergency_exit_flag: bool = False
    broker_risk_flag: bool = False
    manual_exit_flag: bool = False
    manual_exit_leg_id: Optional[str] = None


@dataclass
class LegDecision:
    leg_id: str
    action: ExitAction
    reason: ExitReason
    detail: str = ""


@dataclass
class Decision:
    trade_id: str
    continue_trade: bool
    exit_reason: ExitReason = ExitReason.NONE
    priority: Optional[ExitPriority] = None
    close_entire_trade: bool = False
    leg_decisions: list = field(default_factory=list)   # list[LegDecision]
    pnl_at_decision: float = 0.0
    audit: dict = field(default_factory=dict)
