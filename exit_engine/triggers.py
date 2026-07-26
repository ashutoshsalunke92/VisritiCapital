"""
Concrete ITriggerSource implementations. One class per TriggerSource enum
value -- policies never compute these values themselves, they always ask a
trigger source, which is what makes the whole engine config-swappable.
"""
from exit_engine.interfaces import ITriggerSource
from exit_engine.models import Trade


class EntryPremiumTrigger(ITriggerSource):
    """The trade's immutable entry premium -- SL/TP measured as a % or
    rupee/point move away from this fixed reference."""

    def value_for_trade(self, trade: Trade) -> float:
        return trade.entry_premium if trade.entry_premium is not None else 0.0

    def value_for_leg(self, trade: Trade, leg_id: str) -> float:
        leg = trade.position.legs[leg_id]
        return leg.entry_premium


class LivePnLTrigger(ITriggerSource):
    """Current realized+unrealized P&L -- the most common SL/TP basis."""

    def value_for_trade(self, trade: Trade) -> float:
        return trade.current_pnl()

    def value_for_leg(self, trade: Trade, leg_id: str) -> float:
        leg = trade.position.legs[leg_id]
        return leg.unrealized_pnl()


class MarginUsedTrigger(ITriggerSource):
    """Margin isn't tracked per-leg in this domain model (it's a
    broker/account-level concept) -- value_for_leg falls back to the
    trade's total. Documented simplification: correct for a single-leg
    trade, and a placeholder for multi-leg until a broker margin API is
    wired in (see MarginUsedTrigger docstring in the exit engine README)."""

    def value_for_trade(self, trade: Trade) -> float:
        return trade.margin_used

    def value_for_leg(self, trade: Trade, leg_id: str) -> float:
        return trade.margin_used


class CapitalAllocatedTrigger(ITriggerSource):
    def value_for_trade(self, trade: Trade) -> float:
        return trade.capital_allocated

    def value_for_leg(self, trade: Trade, leg_id: str) -> float:
        return trade.capital_allocated


class PremiumTrigger(ITriggerSource):
    """Current live premium (not P&L, not entry) -- e.g. "exit if premium
    crosses X rupees" style leg-level rules."""

    def value_for_trade(self, trade: Trade) -> float:
        return trade.current_position_premium()

    def value_for_leg(self, trade: Trade, leg_id: str) -> float:
        return trade.position.legs[leg_id].current_premium


TRIGGER_REGISTRY = {
    "ENTRY_PREMIUM": EntryPremiumTrigger(),
    "LIVE_PNL": LivePnLTrigger(),
    "MARGIN_USED": MarginUsedTrigger(),
    "CAPITAL_ALLOCATED": CapitalAllocatedTrigger(),
    "PREMIUM": PremiumTrigger(),
}


def get_trigger_source(trigger_source_enum) -> ITriggerSource:
    return TRIGGER_REGISTRY[trigger_source_enum.value]
