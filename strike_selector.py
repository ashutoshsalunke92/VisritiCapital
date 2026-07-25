"""
Strike Selection Plugins (PDF Module 04).

Two selectors:
  - ATMCondorSelector       : Iron Fly — unchanged, ATM fixed-offset wings.
  - ShortStrangleHedgedSelector : renamed from HedgedDeltaStrangleSelector.
      Key rule changes (v3):
        * Default short-leg target delta = 15 (was 25). Configurable via
          STRANGLE_SHORT_DELTA in .env.
        * Strike selection uses ONLY exchange-listed strikes from the broker
          option chain — never generates a custom/computed strike number.
        * Fallback rule: if no strike has exactly the target delta, pick the
          closest strike whose delta is HIGHER than the target (i.e. closer
          to ATM, more conservative). Never go lower (further OTM).
        * Hedge legs: unchanged — closest 5-delta on the weekly chain, with
          fixed-points fallback using the nearest listed strike.
      HedgedDeltaStrangleSelector kept as an alias so adjustment_engine.py
      and trading_engine.py don't need a rename pass yet.
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


class ShortStrangleHedgedSelector:
    """
    Short Strangle Hedged strike selector (renamed from HedgedDeltaStrangleSelector).

    SHORT LEG SELECTION RULES (v3):
      1. Default target delta = 15 (configurable via STRANGLE_SHORT_DELTA in .env).
      2. Only uses strikes that actually exist in the broker's option chain.
         Never computes or generates a strike number — if it's not in chain.rows,
         it's not used.
      3. Fallback when no strike has exactly the target delta:
         Pick the strike with the NEXT HIGHER delta (closer to ATM).
         Never go further OTM (lower delta) — that would mean less premium
         for the same or more risk.
         Example: target=15, available deltas=[12, 18, 22] → pick 18 (next higher).

    HEDGE LEG SELECTION RULES (unchanged):
      - Closest 5-delta strike on the weekly chain, OTM of the short strike.
      - Fallback: nearest listed strike at fallback_points distance.
      - Never generates a custom strike — only uses chain.rows.

    Both entry and roll calls use the same per-side primitives below, so a
    roll can never price a hedge off the wrong expiry.
    """

    def __init__(self, short_leg_target_delta: float = 15, delta_tolerance: float = 5,
                 hedge_target_delta: float = 5, fallback_points: float = 350,
                 r: float = 0.065):
        # Default changed from 25 → 15. delta_tolerance widened from 3 → 5
        # because at 15-delta the chain is sparser; a tight ±3 tolerance would
        # reject too many valid trading days.
        self.short_leg_target_delta = short_leg_target_delta
        self.delta_tolerance = delta_tolerance
        self.hedge_target_delta = hedge_target_delta
        self.fallback_points = fallback_points
        self.r = r

    def _resolve_deltas(self, chain: OptionChain, option_type: str) -> list:
        """Returns [(strike, premium, delta_or_None), ...] for every row in
        chain.rows. Only uses exchange-listed strikes — no strike is invented here."""
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

    def _select_short_strike(self, resolved: list, target_delta: float,
                              option_type: str) -> SelectedLeg:
        """
        Short-leg selection: closest-higher-delta rule.

        From all strikes with a resolvable delta:
          1. Try to find strikes whose delta >= target (higher delta = closer to ATM).
          2. Among those, pick the one whose delta is CLOSEST to the target
             (i.e. the lowest delta that still meets or exceeds the target).
          3. If no strike meets or exceeds the target (all are further OTM),
             fall back to the strike with the highest available delta overall
             (the least-OTM available), and log a warning.

        This guarantees:
          - We always land on a real exchange-listed strike.
          - We never go further OTM than the target if an alternative exists.
          - The selection is deterministic and auditable.
        """
        usable = [(k, p, d) for (k, p, d) in resolved if d is not None]
        if not usable:
            raise GreeksError(
                f"Delta could not be resolved for any {option_type} strike in the "
                f"broker chain — all premiums may be zero/stale (illiquid expiry?)."
            )

        target_frac = target_delta / 100.0

        # Strikes at or above target delta (closer to ATM than target)
        at_or_above = [(k, p, d) for (k, p, d) in usable if d >= target_frac]

        if at_or_above:
            # Pick the one closest to target from above (lowest delta >= target)
            best = min(at_or_above, key=lambda row: row[2] - target_frac)
        else:
            # All available strikes are further OTM than target.
            # Pick the least-OTM (highest delta available) as the best approximation.
            best = max(usable, key=lambda row: row[2])
            print(
                f"  [WARN] No {option_type} strike found at or above {target_delta}Δ. "
                f"Best available: {best[0]} ({best[2]*100:.1f}Δ). "
                f"Using it — consider adjusting STRANGLE_SHORT_DELTA in .env."
            )

        return SelectedLeg(
            strike=best[0], premium=best[1],
            resolved_delta=best[2], method="delta"
        )

    def _select_hedge_strike(self, resolved: list, short_leg: SelectedLeg,
                              option_type: str, chain: OptionChain) -> SelectedLeg:
        """
        Hedge-leg selection: closest to hedge_target_delta (5Δ), OTM of the
        short leg, using only exchange-listed strikes from the hedge chain.
        Falls back to the nearest listed strike at fallback_points distance.
        """
        # Only consider strikes that are OTM relative to the short leg
        if option_type == "CE":
            candidates = [(k, p, d) for (k, p, d) in resolved
                          if d is not None and k > short_leg.strike]
        else:
            candidates = [(k, p, d) for (k, p, d) in resolved
                          if d is not None and k < short_leg.strike]

        if not candidates:
            raise GreeksError(
                f"No OTM-of-short {option_type} candidates with resolvable delta "
                f"on hedge chain (short strike={short_leg.strike})"
            )

        target_frac = self.hedge_target_delta / 100.0
        best = min(candidates, key=lambda row: abs(row[2] - target_frac))
        return SelectedLeg(
            strike=best[0], premium=best[1],
            resolved_delta=best[2], method="delta"
        )

    def _fixed_points_single(self, chain: OptionChain, short_leg: SelectedLeg,
                              option_type: str) -> SelectedLeg:
        """
        Fallback for hedge leg when delta can't be resolved.
        Uses only exchange-listed strikes — finds the nearest listed strike
        to (short_strike ± fallback_points). Never invents a strike number.
        """
        step = chain.strike_step
        target_k = (
            short_leg.strike + round(self.fallback_points / step) * step
            if option_type == "CE"
            else short_leg.strike - round(self.fallback_points / step) * step
        )

        # Find the nearest actually-listed strike to the target
        listed_strikes = [r.strike for r in chain.rows]
        if not listed_strikes:
            raise ValueError("Hedge chain has no listed strikes at all")

        nearest = min(listed_strikes, key=lambda s: abs(s - target_k))
        row = next(r for r in chain.rows if r.strike == nearest)
        premium = row.ce_premium if option_type == "CE" else row.pe_premium

        if nearest != target_k:
            print(
                f"  [INFO] Fixed-points hedge target {target_k} not listed; "
                f"using nearest listed strike {nearest}."
            )

        return SelectedLeg(strike=nearest, premium=premium, method="fixed_points")

    # ------------------------------------------------------------------ #
    # Per-side primitives — used by both entry and adjustment_engine rolls.
    # ------------------------------------------------------------------ #

    def select_single_short(self, short_chain: OptionChain, option_type: str) -> SelectedLeg:
        """Entry point for one short leg. Always uses exchange-listed strikes."""
        resolved = self._resolve_deltas(short_chain, option_type)
        return self._select_short_strike(resolved, self.short_leg_target_delta, option_type)

    def select_single_hedge(self, hedge_chain: OptionChain, short_leg: SelectedLeg,
                             option_type: str) -> SelectedLeg:
        """Entry point for one hedge leg. Falls back to fixed-points if delta
        can't be resolved, but always lands on an exchange-listed strike."""
        try:
            resolved = self._resolve_deltas(hedge_chain, option_type)
            return self._select_hedge_strike(resolved, short_leg, option_type, hedge_chain)
        except (GreeksError, ValueError):
            return self._fixed_points_single(hedge_chain, short_leg, option_type)

    # ------------------------------------------------------------------ #
    # Two-sided entry API — calls the per-side primitives above.
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


# Alias so adjustment_engine.py and trading_engine.py don't need a rename pass yet.
HedgedDeltaStrangleSelector = ShortStrangleHedgedSelector
