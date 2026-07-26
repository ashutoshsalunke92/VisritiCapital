"""
Config loader for the Exit Engine. Trade-level and leg-level policies are
entirely config-driven — no thresholds, triggers, or actions are ever
hardcoded in the policy classes themselves (see policies.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

from exit_engine.models import TriggerSource, ExitType, ExitAction


@dataclass
class ThresholdConfig:
    trigger_source: TriggerSource
    exit_type: ExitType
    sl_value: Optional[float] = None
    tp_value: Optional[float] = None
    trailing_enabled: bool = False
    trail_activate_value: Optional[float] = None
    trail_giveback_pct: float = 0.3
    # basis_source: what a PERCENTAGE threshold is measured against.
    # "watch LIVE_PNL expressed as % of ENTRY_PREMIUM" is different from
    # "watch ENTRY_PREMIUM itself". Defaults to CAPITAL_ALLOCATED if unset.
    basis_source: Optional[TriggerSource] = None


@dataclass
class TradeLevelPolicyConfig:
    enabled: bool = True
    threshold: Optional[ThresholdConfig] = None


@dataclass
class LegLevelPolicyConfig:
    enabled: bool = False
    threshold: Optional[ThresholdConfig] = None
    on_sl_action: ExitAction = ExitAction.EXIT_LEG
    on_tp_action: ExitAction = ExitAction.EXIT_LEG
    close_entire_trade_on_trigger: bool = False
    excluded_leg_names: list = field(default_factory=list)


@dataclass
class ExitPolicyConfig:
    trade_level: TradeLevelPolicyConfig
    leg_level: LegLevelPolicyConfig


def _parse_threshold(raw: dict) -> Optional[ThresholdConfig]:
    if not raw:
        return None
    return ThresholdConfig(
        trigger_source=TriggerSource(raw["trigger_source"]),
        exit_type=ExitType(raw["exit_type"]),
        sl_value=raw.get("sl_value"),
        tp_value=raw.get("tp_value"),
        trailing_enabled=bool(raw.get("trailing_enabled", False)),
        trail_activate_value=raw.get("trail_activate_value"),
        trail_giveback_pct=float(raw.get("trail_giveback_pct", 0.3)),
        basis_source=TriggerSource(raw["basis_source"]) if raw.get("basis_source") else None,
    )


def load_exit_policy_config(path: str) -> ExitPolicyConfig:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Exit policy config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tl_raw = raw.get("trade_level", {})
    trade_level = TradeLevelPolicyConfig(
        enabled=bool(tl_raw.get("enabled", True)),
        threshold=_parse_threshold(tl_raw.get("threshold")),
    )
    if trade_level.enabled and trade_level.threshold is None:
        raise ValueError("trade_level.enabled=true requires a threshold block")

    ll_raw = raw.get("leg_level", {})
    leg_level = LegLevelPolicyConfig(
        enabled=bool(ll_raw.get("enabled", False)),
        threshold=_parse_threshold(ll_raw.get("threshold")),
        on_sl_action=ExitAction(ll_raw.get("on_sl_action", "EXIT_LEG")),
        on_tp_action=ExitAction(ll_raw.get("on_tp_action", "EXIT_LEG")),
        close_entire_trade_on_trigger=bool(ll_raw.get("close_entire_trade_on_trigger", False)),
        excluded_leg_names=list(ll_raw.get("excluded_leg_names", [])),
    )
    if leg_level.enabled and leg_level.threshold is None:
        raise ValueError("leg_level.enabled=true requires a threshold block")

    return ExitPolicyConfig(trade_level=trade_level, leg_level=leg_level)
