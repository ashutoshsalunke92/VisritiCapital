"""
Adjustment Engine (PDF Section 8 / Module 06) — scoped EXCLUSIVELY to the
monthly Hedged 25-Delta Strangle.

FIXES IN THIS REVISION (see TUNING_GUIDE_V2.md #1):
  - execute_adjustment() now takes BOTH short_chain and hedge_chain, and
    _select_fresh_side() prices the new short off short_chain and the new
    hedge off hedge_chain -- previously both came off short_chain, so every
    roll silently replaced the cheap weekly hedge with a monthly-expiry
    contract at the same strike number (wrong instrument, wrong premium).
  - check_triggers() takes the position's CURRENT live strikes (read via
    StrangleState.current_strikes() by the caller), not the strikes it
    entered with. Previously entry_ce_strike/entry_pe_strike were captured
    once and never updated, so delta monitoring went stale after any roll.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from strike_selector import HedgedDeltaStrangleSelector, OptionChain, GreeksError, SelectedLeg
from strangle_strategy import StrangleState


class AdjustmentAction(Enum):
    NONE = "NONE"
    ROLL_UNTESTED_SIDE = "ROLL_UNTESTED_SIDE"
    ROLL_TESTED_SIDE_SAME_EXPIRY = "ROLL_TESTED_SIDE_SAME_EXPIRY"
    ROLL_OUT_IN_TIME = "ROLL_OUT_IN_TIME"
    REDUCE_OR_CLOSE = "REDUCE_OR_CLOSE"


@dataclass
class TriggerState:
    ce_breached: bool
    pe_breached: bool
    ce_delta: Optional[float]
    pe_delta: Optional[float]
    dte_days: float


@dataclass
class AdjustmentRules:
    delta_breach_threshold: float = 40.0
    entry_target_delta: float = 25.0
    max_delta_after_roll: float = 30.0
    no_new_roll_inside_dte: int = 10
    max_adjustments_per_trade: int = 2
    min_net_credit: float = 0.0
    max_debit_pct_capital: float = 0.005
    time_roll_max_count: int = 1


class AdjustmentEngine:
    def __init__(self, rules: AdjustmentRules, selector: HedgedDeltaStrangleSelector,
                 capital_deployed: float, r: float = 0.065):
        self.rules = rules
        self.selector = selector
        self.capital_deployed = capital_deployed
        self.r = r
        self._time_rolls_done = 0

    def check_triggers(self, state: StrangleState, short_chain: OptionChain,
                        current_ce_strike: Optional[float], current_pe_strike: Optional[float],
                        dte_days: float) -> TriggerState:
        """current_ce_strike/current_pe_strike: the LIVE open strikes (caller
        gets these from state.current_strikes()['ce_short'] /
        ['pe_short'] every tick -- NOT the original entry strikes)."""
        ce_delta = pe_delta = None
        ce_row = next((row for row in short_chain.rows if row.strike == current_ce_strike), None) \
            if current_ce_strike is not None else None
        pe_row = next((row for row in short_chain.rows if row.strike == current_pe_strike), None) \
            if current_pe_strike is not None else None

        from greeks_engine import delta_from_premium
        try:
            if ce_row is not None:
                ce_delta = delta_from_premium(ce_row.ce_premium, short_chain.spot,
                                               current_ce_strike, short_chain.t_years, self.r, "CE") * 100
        except GreeksError:
            ce_delta = None
        try:
            if pe_row is not None:
                pe_delta = delta_from_premium(pe_row.pe_premium, short_chain.spot,
                                               current_pe_strike, short_chain.t_years, self.r, "PE") * 100
        except GreeksError:
            pe_delta = None

        ce_delta_breach = ce_delta is not None and ce_delta >= self.rules.delta_breach_threshold
        pe_delta_breach = pe_delta is not None and pe_delta >= self.rules.delta_breach_threshold
        ce_price_breach = current_ce_strike is not None and short_chain.spot >= current_ce_strike
        pe_price_breach = current_pe_strike is not None and short_chain.spot <= current_pe_strike

        return TriggerState(
            ce_breached=ce_delta_breach and ce_price_breach,
            pe_breached=pe_delta_breach and pe_price_breach,
            ce_delta=ce_delta, pe_delta=pe_delta, dte_days=dte_days,
        )

    def next_action(self, state: StrangleState, trigger: TriggerState,
                     event_calendar_active: bool = False) -> AdjustmentAction:
        if event_calendar_active:
            return AdjustmentAction.NONE
        if not (trigger.ce_breached or trigger.pe_breached):
            return AdjustmentAction.NONE
        if trigger.dte_days <= self.rules.no_new_roll_inside_dte:
            return AdjustmentAction.REDUCE_OR_CLOSE
        if state.adjustment_count >= self.rules.max_adjustments_per_trade:
            return AdjustmentAction.REDUCE_OR_CLOSE

        untested_breached = trigger.pe_breached if trigger.ce_breached else trigger.ce_breached
        if not untested_breached:
            return AdjustmentAction.ROLL_UNTESTED_SIDE
        return AdjustmentAction.ROLL_TESTED_SIDE_SAME_EXPIRY

    def execute_adjustment(self, action: AdjustmentAction, state: StrangleState,
                            short_chain: OptionChain, hedge_chain: OptionChain,
                            tested_side: str, ts) -> dict:
        """short_chain/hedge_chain: the SAME two chains used at entry (monthly
        for shorts, weekly for hedges) -- fixed in this revision, previously
        only short_chain was passed and hedges got mispriced on every roll."""
        short_name = f"{tested_side}_short"
        hedge_name = f"{tested_side}_hedge"
        other = "pe" if tested_side == "ce" else "ce"
        other_short_name = f"{other}_short"
        other_hedge_name = f"{other}_hedge"

        if action == AdjustmentAction.ROLL_UNTESTED_SIDE:
            new_short, new_hedge = self._select_fresh_side(short_chain, hedge_chain, other)
            if new_short.resolved_delta is not None and new_short.resolved_delta * 100 > self.rules.max_delta_after_roll:
                return {"status": "SKIPPED", "reason": "post-roll delta exceeds cap"}
            return {"status": "ROLL", "side": other, "short": new_short, "hedge": new_hedge,
                    "short_leg_name": other_short_name, "hedge_leg_name": other_hedge_name}

        if action == AdjustmentAction.ROLL_TESTED_SIDE_SAME_EXPIRY:
            new_short, new_hedge = self._select_fresh_side(short_chain, hedge_chain, tested_side)
            net_credit_from_roll = self._estimate_roll_credit(state, short_chain, hedge_chain,
                                                               tested_side, new_short, new_hedge)
            max_debit = self.capital_deployed * self.rules.max_debit_pct_capital
            if net_credit_from_roll < -max_debit:
                return {"status": "SKIPPED", "reason": "roll would require debit beyond cap"}
            return {"status": "ROLL", "side": tested_side, "short": new_short, "hedge": new_hedge,
                    "short_leg_name": short_name, "hedge_leg_name": hedge_name,
                    "est_credit": net_credit_from_roll}

        if action == AdjustmentAction.ROLL_OUT_IN_TIME:
            if self._time_rolls_done >= self.rules.time_roll_max_count:
                return {"status": "SKIPPED", "reason": "time-roll already used once this trade"}
            self._time_rolls_done += 1
            return {"status": "ROLL_OUT_IN_TIME", "side": tested_side,
                    "note": "requires a next-expiry chain from the caller"}

        if action == AdjustmentAction.REDUCE_OR_CLOSE:
            return {"status": "REDUCE_OR_CLOSE"}

        return {"status": "NONE"}

    def _select_fresh_side(self, short_chain: OptionChain, hedge_chain: OptionChain,
                            side: str) -> tuple:
        """NEW signature: short priced off short_chain, hedge priced off
        hedge_chain -- via the selector's per-side primitives (strike_selector.py),
        exactly mirroring how entry works. This is the actual bug fix."""
        option_type = "CE" if side == "ce" else "PE"
        short = self.selector.select_single_short(short_chain, option_type)
        hedge = self.selector.select_single_hedge(hedge_chain, short, option_type)
        return short, hedge

    def _estimate_roll_credit(self, state: StrangleState, short_chain: OptionChain,
                               hedge_chain: OptionChain, side: str,
                               new_short: SelectedLeg, new_hedge: SelectedLeg) -> float:
        short_name = f"{side}_short"
        hedge_name = f"{side}_hedge"
        old_short = state._open_fill(short_name)
        old_hedge = state._open_fill(hedge_name)
        if old_short is None or old_hedge is None:
            return 0.0
        old_short_mark = next(
            (r.ce_premium if side == "ce" else r.pe_premium
             for r in short_chain.rows if r.strike == old_short.strike), None)
        old_hedge_mark = next(
            (r.ce_premium if side == "ce" else r.pe_premium
             for r in hedge_chain.rows if r.strike == old_hedge.strike), None)
        if old_short_mark is None or old_hedge_mark is None:
            return 0.0  # can't estimate -> caller's net-credit-not-met check treats this conservatively
        close_old_cost = (old_short_mark - old_hedge_mark)
        open_new_credit = (new_short.premium - new_hedge.premium)
        return (open_new_credit - close_old_cost) * state.quantity
