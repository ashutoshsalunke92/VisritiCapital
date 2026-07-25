"""
Strike Selection Plugins (PDF Module 04).

CHANGE IN THIS REVISION: HedgedDeltaStrangleSelector now exposes per-side,
per-chain methods (select_single_short / select_single_hedge) alongside the
original select_short_legs/select_hedge_legs. This is what fixes the
adjustment-engine bug where a roll picked BOTH the new short and the new
hedge off the same (monthly) chain — after a roll the hedge leg was being
priced off the wrong expiry entirely. adjustment_engine.py now calls the
per-side methods explicitly with the correct chain for each leg, exactly
mirroring how entry already works (short off the monthly chain, hedge off
the weekly chain).
"""
from dataclasses import dataclass
from typing import Optional, Protocol

from greeks_engine import delta_from_premium, GreeksError


@dataclass
class ChainRow:
    strike: float
    ce_premium: float
    pe_premium: float


@dataclass
class OptionChain:
    underlying: str
    expiry_date: str
    spot: float
    t_years: float
    rows: list
    strike_step: int


@dataclass
class SelectedLeg:
    strike: float
    premium: float
    resolved_delta: Optional[float] = None
    method: str = "delta"


class StrikeSelector(Protocol):
    def select_short_legs(self, chain: OptionChain) -> tuple: ...
    def select_hedge_legs(self, chain: OptionChain, ce_short: SelectedLeg,
                           pe_short: SelectedLeg) -> tuple: ...


class ATMCondorSelector:
    """Unchanged -- describes, but is not wired into, the existing Iron
    Fly's real code path (data_fetcher.py / strategy.py / backtest_engine.py
    keep running exactly as before)."""

    def __init__(self, otm_wing_strikes: int, strike_step: int):
        self.otm_wing_strikes = otm_wing_strikes
        self.strike_step = strike_step

    def select_short_legs(self, chain: OptionChain) -> tuple:
        atm = round(chain.spot / chain.strike_step) * chain.strike_step
        row = next((r for r in chain.rows if r.strike == atm), None)
        if row is None:
            raise ValueError(f"ATM strike {atm} not found in chain")
        ce = SelectedLeg(strike=atm, premium=row.ce_premium, method="atm")
        pe = SelectedLeg(strike=atm, premium=row.pe_premium, method="atm")
        return ce, pe

    def select_hedge_legs(self, chain: OptionChain, ce_short, pe_short) -> tuple:
        offset = self.otm_wing_strikes * self.strike_step
        ce_k = ce_short.strike + offset
        pe_k = pe_short.strike - offset
        ce_row = next((r for r in chain.rows if r.strike == ce_k), None)
        pe_row = next((r for r in chain.rows if r.strike == pe_k), None)
        if ce_row is None or pe_row is None:
            raise ValueError(f"Wing strikes {ce_k}/{pe_k} not found in chain")
        ce = SelectedLeg(strike=ce_k, premium=ce_row.ce_premium, method="fixed_points")
        pe = SelectedLeg(strike=pe_k, premium=pe_row.pe_premium, method="fixed_points")
        return ce, pe


class HedgedDeltaStrangleSelector:
    """Short legs: monthly chain, closest |delta| to short_leg_target_delta.
    Hedge legs: weekly chain, closest |delta| to hedge_target_delta (or a
    fixed-points fallback off that SAME weekly chain if delta can't be
    resolved). Both the initial entry AND every subsequent roll go through
    the exact same per-side methods below, so a roll can never end up
    pricing a hedge off the wrong expiry again."""

    def __init__(self, short_leg_target_delta: float = 25, delta_tolerance: float = 3,
                 hedge_target_delta: float = 5, fallback_points: float = 350,
                 r: float = 0.065):
        self.short_leg_target_delta = short_leg_target_delta
        self.delta_tolerance = delta_tolerance
        self.hedge_target_delta = hedge_target_delta
        self.fallback_points = fallback_points
        self.r = r

    def _resolve_deltas(self, chain: OptionChain, option_type: str) -> list:
        out = []
        for row in chain.rows:
            premium = row.ce_premium if option_type == "CE" else row.pe_premium
            try:
                d = delta_from_premium(premium, chain.spot, row.strike, chain.t_years,
                                        self.r, option_type)
            except GreeksError:
                d = None
            out.append((row.strike, premium, d))
        return out

    def _closest_to_target(self, resolved: list, target_delta: float,
                            tolerance: Optional[float]) -> SelectedLeg:
        usable = [(k, p, d) for (k, p, d) in resolved if d is not None]
        if not usable:
            raise GreeksError("Delta could not be resolved for any strike on this "
                               "side of the chain (illiquid/stale premiums).")
        target_frac = target_delta / 100.0
        best = min(usable, key=lambda row: abs(row[2] - target_frac))
        if tolerance is not None and abs(best[2] - target_frac) * 100 > tolerance:
            raise ValueError(
                f"Closest available delta ({best[2]*100:.1f}) is outside tolerance "
                f"±{tolerance} of target {target_delta}")
        return SelectedLeg(strike=best[0], premium=best[1], resolved_delta=best[2], method="delta")

    # ------------------------------------------------------------------ #
    # NEW: per-side, per-chain primitives -- used by both entry (below) and
    # adjustment_engine.py's rolls, so both paths are identical.
    # ------------------------------------------------------------------ #

    def select_single_short(self, short_chain: OptionChain, option_type: str) -> SelectedLeg:
        resolved = self._resolve_deltas(short_chain, option_type)
        return self._closest_to_target(resolved, self.short_leg_target_delta, self.delta_tolerance)

    def select_single_hedge(self, hedge_chain: OptionChain, short_leg: SelectedLeg,
                             option_type: str) -> SelectedLeg:
        try:
            resolved = self._resolve_deltas(hedge_chain, option_type)
            if option_type == "CE":
                candidates = [(k, p, d) for (k, p, d) in resolved if d is not None and k > short_leg.strike]
            else:
                candidates = [(k, p, d) for (k, p, d) in resolved if d is not None and k < short_leg.strike]
            if not candidates:
                raise GreeksError("No OTM-of-short candidates with resolvable delta on hedge chain")
            return self._closest_to_target(candidates, self.hedge_target_delta, None)
        except (GreeksError, ValueError):
            return self._fixed_points_single(hedge_chain, short_leg, option_type)

    def _fixed_points_single(self, chain: OptionChain, short_leg: SelectedLeg,
                              option_type: str) -> SelectedLeg:
        step = chain.strike_step
        offset = round(self.fallback_points / step) * step
        k = short_leg.strike + offset if option_type == "CE" else short_leg.strike - offset
        row = next((r for r in chain.rows if r.strike == k), None)
        if row is None:
            raise ValueError(f"Fixed-points fallback strike {k} not found in hedge chain")
        premium = row.ce_premium if option_type == "CE" else row.pe_premium
        return SelectedLeg(strike=k, premium=premium, method="fixed_points")

    # ------------------------------------------------------------------ #
    # Original two-sided API (entry) -- now implemented on top of the
    # per-side primitives above, so there's exactly one code path.
    # ------------------------------------------------------------------ #

    def select_short_legs(self, chain: OptionChain) -> tuple:
        ce = self.select_single_short(chain, "CE")
        pe = self.select_single_short(chain, "PE")
        return ce, pe

    def select_hedge_legs(self, chain: OptionChain, ce_short: SelectedLeg,
                           pe_short: SelectedLeg) -> tuple:
        ce = self.select_single_hedge(chain, ce_short, "CE")
        pe = self.select_single_hedge(chain, pe_short, "PE")
        return ce, pe
