"""
Public entry point: TradeExitEngine.evaluate_trade(context) -> Decision.

Everything upstream of this file (the specific strategy, the backtest
replay loop, the live poller) only ever needs to know this one method. No
strategy-specific business logic lives here or anywhere else in
exit_engine/ -- it evaluates whatever policies the config wired in, against
whatever numbers the caller put in the TradeContext this tick.
"""
from __future__ import annotations

from typing import Optional

from exit_engine.audit import AuditLogger
from exit_engine.config import ExitPolicyConfig, load_exit_policy_config
from exit_engine.decision_manager import ExitDecisionManager
from exit_engine.models import TradeContext, Decision, ExitReason
from exit_engine.policies import TradeLevelExitPolicy, LegLevelExitPolicy, FlagExitPolicy


class TradeExitEngine:
    def __init__(self, config: ExitPolicyConfig, audit_logger: Optional[AuditLogger] = None):
        self.config = config
        policies = [
            # priorities 1-3 -- always registered, gated by the context flags per-call
            FlagExitPolicy("emergency_exit_flag", ExitReason.EMERGENCY_EXIT),
            FlagExitPolicy("broker_risk_flag", ExitReason.BROKER_RISK_EXIT),
            FlagExitPolicy("manual_exit_flag", ExitReason.MANUAL_EXIT),
            TradeLevelExitPolicy(config.trade_level),
            LegLevelExitPolicy(config.leg_level),
        ]
        self._manager = ExitDecisionManager(policies, audit_logger=audit_logger)

    @classmethod
    def from_yaml(cls, path: str, audit_logger: Optional[AuditLogger] = None) -> "TradeExitEngine":
        return cls(load_exit_policy_config(path), audit_logger=audit_logger)

    def register_policy(self, policy):
        """Extension point -- add a new IExitPolicy plugin at runtime without
        touching this class (Strategy/Plugin pattern, per spec)."""
        self._manager.register_policy(policy)

    def evaluate_trade(self, context: TradeContext) -> Decision:
        return self._manager.decide(context)
