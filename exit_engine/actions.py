"""
Concrete IExitAction implementations. Leg-level policies trigger one of
these; trade-level SL/TP never needs one (it always closes everything
directly via the Decision Manager).
"""
from exit_engine.interfaces import IExitAction
from exit_engine.models import Trade, TradeContext, ExitAction, new_leg_id


class ExitLegAction(IExitAction):
    @property
    def action_type(self) -> ExitAction:
        return ExitAction.EXIT_LEG

    def apply(self, trade: Trade, leg_id: str, context: TradeContext) -> None:
        leg = trade.position.legs[leg_id]
        leg.current_premium = context.leg_marks.get(leg_id, leg.current_premium)
        trade.realized_pnl += leg.unrealized_pnl()
        leg.is_open = False


class RollLegAction(IExitAction):
    """Archives the old leg (kept for audit, is_open=False, archived=True)
    and expects the caller to register a NEW leg under a new LegID via
    trade.position.legs[new_id] = Leg(...) -- this action only performs the
    archival half; strike/premium selection for the replacement is strategy
    logic the engine deliberately doesn't own (per spec: "no strategy-
    specific business logic" in the engine)."""

    @property
    def action_type(self) -> ExitAction:
        return ExitAction.ROLL_LEG

    def apply(self, trade: Trade, leg_id: str, context: TradeContext) -> None:
        leg = trade.position.legs[leg_id]
        leg.current_premium = context.leg_marks.get(leg_id, leg.current_premium)
        trade.realized_pnl += leg.unrealized_pnl()
        leg.is_open = False
        leg.archived = True
        trade.adjustment_count += 1


class ReplaceLegAction(RollLegAction):
    """Same mechanics as a roll (archive + realize P&L) -- kept as a
    distinct action type because callers may want different downstream
    behavior (e.g. a replace targets a different OPTION TYPE/strategy leg
    role, not just a new strike on the same role) even though the engine's
    own bookkeeping is identical."""

    @property
    def action_type(self) -> ExitAction:
        return ExitAction.REPLACE_LEG


class NotifyOnlyAction(IExitAction):
    """No state change -- just marks that this trigger fired, for audit/
    notification purposes. The leg stays open."""

    @property
    def action_type(self) -> ExitAction:
        return ExitAction.NOTIFY_ONLY

    def apply(self, trade: Trade, leg_id: str, context: TradeContext) -> None:
        pass  # deliberately a no-op; audit logging happens in the Decision Manager


class CloseEntireTradeAction(IExitAction):
    """Closes every open leg -- used when a leg-level trigger has
    close_entire_trade_on_trigger=True configured."""

    @property
    def action_type(self) -> ExitAction:
        return ExitAction.CLOSE_ENTIRE_TRADE

    def apply(self, trade: Trade, leg_id: str, context: TradeContext) -> None:
        for l in trade.position.open_legs():
            l.current_premium = context.leg_marks.get(l.leg_id, l.current_premium)
            trade.realized_pnl += l.unrealized_pnl()
            l.is_open = False


ACTION_REGISTRY = {
    ExitAction.EXIT_LEG: ExitLegAction(),
    ExitAction.ROLL_LEG: RollLegAction(),
    ExitAction.REPLACE_LEG: ReplaceLegAction(),
    ExitAction.NOTIFY_ONLY: NotifyOnlyAction(),
    ExitAction.CLOSE_ENTIRE_TRADE: CloseEntireTradeAction(),
}


def get_action(action_enum: ExitAction) -> IExitAction:
    return ACTION_REGISTRY[action_enum]
