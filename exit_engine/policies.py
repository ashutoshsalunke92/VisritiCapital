"""
IExitPolicy implementations.

Threshold convention (documented here since the spec leaves the exact unit
semantics open):
  - exit_type=PERCENTAGE: the sl_value/tp_value fraction is applied against
    a basis. For LIVE_PNL/MARGIN_USED/CAPITAL_ALLOCATED triggers the basis
    is trade.capital_allocated (e.g. "SL at 4% of capital allocated" --
    matches this project's existing capital-based convention). For
    ENTRY_PREMIUM/PREMIUM triggers the basis is trade.entry_premium (e.g.
    "SL at 25% adverse move on premium" -- matches the existing per-leg
    convention).
  - exit_type=RUPEES / PREMIUM_POINTS: sl_value/tp_value is used directly
    as an absolute threshold, no scaling.
  - Trailing: trail_activate_value is in the SAME unit/basis as tp_value.
    Once the trigger value reaches the activation threshold, the fixed TP
    is replaced by a trailing stop that ratchets up behind the peak and
    exits on trail_giveback_pct pullback from that peak. The SL always
    stays live underneath as a hard floor, exactly as in this project's
    existing trailing-SL implementations (strategy.py / strangle_strategy.py).
"""
from __future__ import annotations

from typing import Optional

from exit_engine.config import ThresholdConfig, TradeLevelPolicyConfig, LegLevelPolicyConfig
from exit_engine.interfaces import IExitPolicy
from exit_engine.models import (Trade, TradeContext, Decision, LegDecision, ExitReason,
                                 ExitAction, ExitType, TriggerSource)
from exit_engine.triggers import get_trigger_source


def _basis_for(trade: Trade, threshold: ThresholdConfig) -> float:
    """Resolves the PERCENTAGE basis. Uses threshold.basis_source when the
    config sets it explicitly; otherwise falls back to inferring from
    trigger_source (backward-compatible default for configs written before
    basis_source existed)."""
    source = threshold.basis_source or threshold.trigger_source
    if source == TriggerSource.ENTRY_PREMIUM:
        return trade.entry_premium if trade.entry_premium else 0.0
    if source == TriggerSource.CAPITAL_ALLOCATED:
        return trade.capital_allocated
    if source == TriggerSource.MARGIN_USED:
        return trade.margin_used
    # LIVE_PNL / PREMIUM as a basis doesn't make sense (they're the moving
    # value, not a fixed reference) -- fall back to capital_allocated.
    return trade.capital_allocated


def _resolve_thresholds(basis: float, threshold: ThresholdConfig) -> tuple:
    """Returns (sl_threshold, tp_threshold, activate_threshold) as signed/
    absolute values ready to compare directly against the trigger's raw
    value. sl_threshold is negative (a loss level), tp_threshold and
    activate_threshold are positive (profit levels)."""
    if threshold.exit_type == ExitType.PERCENTAGE:
        sl = -abs(basis) * threshold.sl_value if threshold.sl_value is not None else None
        tp = abs(basis) * threshold.tp_value if threshold.tp_value is not None else None
        act = abs(basis) * threshold.trail_activate_value if threshold.trail_activate_value is not None else None
    else:  # RUPEES / PREMIUM_POINTS -- absolute values, no scaling
        sl = -abs(threshold.sl_value) if threshold.sl_value is not None else None
        tp = abs(threshold.tp_value) if threshold.tp_value is not None else None
        act = abs(threshold.trail_activate_value) if threshold.trail_activate_value is not None else None
    return sl, tp, act


class TradeLevelExitPolicy(IExitPolicy):
    def __init__(self, config: TradeLevelPolicyConfig):
        self._config = config

    @property
    def name(self) -> str:
        return "TradeLevelExitPolicy"

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def evaluate(self, context: TradeContext) -> Optional[Decision]:
        if not self.enabled or self._config.threshold is None:
            return None
        trade = context.trade
        th = self._config.threshold
        trigger = get_trigger_source(th.trigger_source)
        value = trigger.value_for_trade(trade)
        basis = _basis_for(trade, th)
        sl_threshold, tp_threshold, activate_threshold = _resolve_thresholds(basis, th)

        # Hard SL always evaluated first, even once trailing is armed --
        # trailing can only tighten risk, never widen it past the static SL.
        if sl_threshold is not None and value <= sl_threshold:
            return Decision(trade_id=trade.trade_id, continue_trade=False,
                             exit_reason=ExitReason.TRADE_LEVEL_SL, close_entire_trade=True,
                             pnl_at_decision=value,
                             audit={"trigger_source": th.trigger_source.value, "value": value,
                                    "threshold": sl_threshold})

        if th.trailing_enabled and activate_threshold is not None:
            if not trade.trailing_armed and value >= activate_threshold:
                trade.trailing_armed = True
                trade.peak_value = value
            if trade.trailing_armed:
                trade.peak_value = max(trade.peak_value, value)
                trail_stop = trade.peak_value * (1 - th.trail_giveback_pct)
                if value <= trail_stop:
                    return Decision(trade_id=trade.trade_id, continue_trade=False,
                                     exit_reason=ExitReason.TRADE_LEVEL_TRAILING_SL, close_entire_trade=True,
                                     pnl_at_decision=value,
                                     audit={"trigger_source": th.trigger_source.value, "value": value,
                                            "peak": trade.peak_value, "trail_stop": trail_stop})
            return None  # trailing mode active -- fixed TP suppressed once armed, per module docstring

        if tp_threshold is not None and value >= tp_threshold:
            return Decision(trade_id=trade.trade_id, continue_trade=False,
                             exit_reason=ExitReason.TRADE_LEVEL_TP, close_entire_trade=True,
                             pnl_at_decision=value,
                             audit={"trigger_source": th.trigger_source.value, "value": value,
                                    "threshold": tp_threshold})
        return None


class LegLevelExitPolicy(IExitPolicy):
    def __init__(self, config: LegLevelPolicyConfig):
        self._config = config

    @property
    def name(self) -> str:
        return "LegLevelExitPolicy"

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def evaluate(self, context: TradeContext) -> Optional[Decision]:
        if not self.enabled or self._config.threshold is None:
            return None
        trade = context.trade
        th = self._config.threshold
        trigger = get_trigger_source(th.trigger_source)

        leg_decisions = []
        for leg in trade.position.open_legs():
            if leg.name in self._config.excluded_leg_names:
                continue
            value = trigger.value_for_leg(trade, leg.leg_id)
            basis = leg.entry_premium if th.basis_source in (None, TriggerSource.ENTRY_PREMIUM) \
                else _basis_for(trade, th)
            sl_threshold, tp_threshold, activate_threshold = _resolve_thresholds(basis, th)

            if sl_threshold is not None and value <= sl_threshold:
                leg_decisions.append(LegDecision(leg_id=leg.leg_id, action=self._config.on_sl_action,
                                                  reason=ExitReason.LEG_LEVEL_SL,
                                                  detail=f"value={value} <= sl_threshold={sl_threshold}"))
                continue

            if th.trailing_enabled and activate_threshold is not None:
                if not leg.trailing_armed and value >= activate_threshold:
                    leg.trailing_armed = True
                    leg.peak_value = value
                if leg.trailing_armed:
                    leg.peak_value = max(leg.peak_value, value)
                    trail_stop = leg.peak_value * (1 - th.trail_giveback_pct)
                    if value <= trail_stop:
                        leg_decisions.append(LegDecision(leg_id=leg.leg_id, action=self._config.on_sl_action,
                                                          reason=ExitReason.LEG_LEVEL_TRAILING_SL,
                                                          detail=f"value={value} peak={leg.peak_value} trail_stop={trail_stop}"))
                continue

            if tp_threshold is not None and value >= tp_threshold:
                leg_decisions.append(LegDecision(leg_id=leg.leg_id, action=self._config.on_tp_action,
                                                  reason=ExitReason.LEG_LEVEL_TP,
                                                  detail=f"value={value} >= tp_threshold={tp_threshold}"))

        if not leg_decisions:
            return None

        close_all = self._config.close_entire_trade_on_trigger or \
            any(ld.action == ExitAction.CLOSE_ENTIRE_TRADE for ld in leg_decisions)
        primary_reason = leg_decisions[0].reason
        return Decision(trade_id=trade.trade_id, continue_trade=False, exit_reason=primary_reason,
                         close_entire_trade=close_all, leg_decisions=leg_decisions,
                         pnl_at_decision=trade.current_pnl(),
                         audit={"leg_count_triggered": len(leg_decisions)})


class FlagExitPolicy(IExitPolicy):
    """Covers priorities 1-3 (Emergency / Broker Risk / Manual) -- all three
    are simple boolean flags on TradeContext rather than threshold math, but
    they're still ordinary IExitPolicy plugins so the Decision Manager
    treats them through the exact same evaluate()+priority path as
    everything else -- no special-casing anywhere else in the engine."""

    def __init__(self, flag_attr: str, reason: ExitReason, always_enabled: bool = True):
        self._flag_attr = flag_attr
        self._reason = reason
        self._enabled = always_enabled

    @property
    def name(self) -> str:
        return f"FlagExitPolicy[{self._reason.value}]"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def evaluate(self, context: TradeContext) -> Optional[Decision]:
        if not getattr(context, self._flag_attr, False):
            return None
        leg_id = getattr(context, "manual_exit_leg_id", None)
        close_all = leg_id is None
        leg_decisions = [] if close_all else [
            LegDecision(leg_id=leg_id, action=ExitAction.EXIT_LEG, reason=self._reason)]
        return Decision(trade_id=context.trade.trade_id, continue_trade=False, exit_reason=self._reason,
                         close_entire_trade=close_all, leg_decisions=leg_decisions,
                         pnl_at_decision=context.trade.current_pnl(), audit={"flag": self._flag_attr})
