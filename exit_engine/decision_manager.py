"""
ExitDecisionManager -- the centralized, thread-safe arbiter. Every exit
request (from any policy) routes through here. Per spec section 6:

  - Acquires a per-trade lock (keyed by TradeID) before executing anything,
    so only one decision is ever applied to a given trade at a time, but
    unrelated trades never block each other.
  - If multiple policies fire in the same evaluation, the highest-priority
    one wins (ExitPriority, ascending = higher priority) and its action is
    the only one applied.
  - Once a trade is CLOSED, further decide() calls are no-ops (duplicate
    exits / duplicate orders are structurally impossible, not just
    discouraged) -- the caller gets back a harmless CONTINUE-shaped decision
    instead of the engine trying to re-close an already-closed trade.
"""
from __future__ import annotations

import threading
from typing import Optional

from exit_engine.actions import get_action
from exit_engine.audit import AuditLogger
from exit_engine.interfaces import IExitPolicy
from exit_engine.models import (Trade, TradeContext, Decision, ExitAction, ExitPriority,
                                 ExitReason, TradeState)


class ExitDecisionManager:
    def __init__(self, policies: list, audit_logger: Optional[AuditLogger] = None):
        self._policies: list = list(policies)
        self._master_lock = threading.Lock()
        self._trade_locks: dict = {}
        self._closed_trade_ids: set = set()
        self._audit = audit_logger or AuditLogger()

    def register_policy(self, policy: IExitPolicy):
        with self._master_lock:
            self._policies.append(policy)

    def _lock_for(self, trade_id: str) -> threading.Lock:
        with self._master_lock:
            lock = self._trade_locks.get(trade_id)
            if lock is None:
                lock = threading.Lock()
                self._trade_locks[trade_id] = lock
            return lock

    def decide(self, context: TradeContext) -> Decision:
        trade = context.trade
        lock = self._lock_for(trade.trade_id)

        with lock:
            if trade.trade_id in self._closed_trade_ids or trade.state == TradeState.CLOSED:
                decision = Decision(trade_id=trade.trade_id, continue_trade=True,
                                     exit_reason=ExitReason.NONE,
                                     audit={"duplicate_suppressed": True})
                self._audit.log({"trade_id": trade.trade_id, "duplicate_suppressed": True,
                                  "as_of": context.as_of})
                return decision

            candidates = []
            considered = []
            for policy in self._policies:
                if not policy.enabled:
                    continue
                try:
                    d = policy.evaluate(context)
                except Exception as e:
                    considered.append({"policy": policy.name, "error": str(e)})
                    continue
                if d is not None:
                    d.priority = d.priority or ExitPriority.for_reason(d.exit_reason)
                    candidates.append((policy.name, d))
                    considered.append({"policy": policy.name, "fired": True,
                                        "reason": d.exit_reason.value, "priority": d.priority.value})
                else:
                    considered.append({"policy": policy.name, "fired": False})

            if not candidates:
                self._audit.log({"trade_id": trade.trade_id, "decision": "CONTINUE",
                                  "as_of": context.as_of, "policies_considered": considered})
                return Decision(trade_id=trade.trade_id, continue_trade=True,
                                 pnl_at_decision=trade.current_pnl())

            # Highest priority wins (lowest ExitPriority.value = highest priority).
            candidates.sort(key=lambda item: item[1].priority.value)
            winning_policy_name, winner = candidates[0]

            self._apply_decision(trade, winner, context)

            if winner.close_entire_trade or not trade.position.open_legs():
                trade.state = TradeState.CLOSED
                self._closed_trade_ids.add(trade.trade_id)
            elif trade.adjustment_count > 0:
                trade.state = TradeState.ADJUSTED

            conflicting = [{"policy": n, "reason": d.exit_reason.value} for n, d in candidates[1:]]
            self._audit.log({
                "trade_id": trade.trade_id, "decision": "EXIT", "as_of": context.as_of,
                "winning_policy": winning_policy_name, "exit_reason": winner.exit_reason.value,
                "priority": winner.priority.value, "close_entire_trade": winner.close_entire_trade,
                "pnl_at_decision": winner.pnl_at_decision,
                "leg_decisions": [{"leg_id": ld.leg_id, "action": ld.action.value, "reason": ld.reason.value}
                                   for ld in winner.leg_decisions],
                "conflicting_candidates": conflicting,
                "policies_considered": considered,
                "trade_state_after": trade.state.value,
            })
            return winner

    def _apply_decision(self, trade: Trade, decision: Decision, context: TradeContext):
        """Applies the winning decision's effect. Trade-level SL/TP (no
        explicit leg_decisions, close_entire_trade=True) closes every open
        leg via ExitLegAction; leg-level decisions apply their configured
        action per leg, then close-the-rest if close_entire_trade_on_trigger
        was set."""
        if not decision.leg_decisions:
            action = get_action(ExitAction.EXIT_LEG)
            for leg in list(trade.position.open_legs()):
                action.apply(trade, leg.leg_id, context)
            return

        for ld in decision.leg_decisions:
            action = get_action(ld.action)
            action.apply(trade, ld.leg_id, context)

        if decision.close_entire_trade:
            close_action = get_action(ExitAction.CLOSE_ENTIRE_TRADE)
            close_action.apply(trade, None, context)
