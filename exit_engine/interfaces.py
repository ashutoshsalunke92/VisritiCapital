"""
Core interfaces (Strategy/Plugin pattern). Everything above the trigger/
policy/action layer codes against these, never a concrete class -- adding a
new trigger source, policy, or exit action means writing one new class, zero
changes to the Decision Manager or the public evaluate_trade() API.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from exit_engine.models import Trade, TradeContext, Decision, ExitAction


class ITriggerSource(ABC):
    """Resolves a single numeric value for one trigger source (EntryPremium,
    LivePnL, MarginUsed, CapitalAllocated, Premium) for a trade or a leg.
    Pure computation, no side effects, no I/O."""

    @abstractmethod
    def value_for_trade(self, trade: Trade) -> float: ...

    @abstractmethod
    def value_for_leg(self, trade: Trade, leg_id: str) -> float: ...


class IExitPolicy(ABC):
    """One policy = one evaluation pass over a TradeContext. Trade-level and
    leg-level SL/TP are both IExitPolicy implementations; so is any future
    policy (e.g. a volatility-spike policy) -- the Decision Manager doesn't
    care which, it just calls evaluate() and applies priority ordering to
    whatever comes back."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def evaluate(self, context: TradeContext) -> Optional[Decision]:
        """Returns None if this policy doesn't want to act this tick, or a
        Decision (continue_trade=False, ...) if it does. Never mutates the
        Trade directly -- the Decision Manager / IExitAction layer owns
        actually applying state changes, so evaluate() stays side-effect-free
        and safe to call speculatively / in parallel across policies."""
        ...


class IExitAction(ABC):
    """Applies the effect of a triggered leg-level policy (ExitLeg, RollLeg,
    ReplaceLeg, NotifyOnly, CloseEntireTrade). Trade-level SL/TP always just
    closes everything, so it doesn't need this -- only leg-level policies
    route through an IExitAction, since they have more than one possible
    response to a trigger."""

    @property
    @abstractmethod
    def action_type(self) -> ExitAction: ...

    @abstractmethod
    def apply(self, trade: Trade, leg_id: str, context: TradeContext) -> None: ...
