"""
Black-Scholes IV/Delta engine — new module, used by strike_selector.py (to find
25-delta/5-delta strikes) and adjustment_engine.py (to watch live short-leg delta
for the monthly strangle's adjustment triggers).

Pure stdlib (`math`), no scipy dependency. Primary path: solve IV from an observed
option premium via bisection (robust and simple to reason about; Newton-Raphson can
diverge on deep-OTM/near-expiry contracts where vega is tiny, which is exactly the
hedge-leg case we care most about getting right), then compute delta from that IV.

Fallback path: if IV can't be solved (premium outside any no-arbitrage IV range,
zero/negative premium, or numerical failure), callers should catch GreeksError and
fall back to a fixed-points wing rule — see strike_selector.py. This module never
guesses or returns a fabricated delta; it raises instead.
"""
import math
from dataclasses import dataclass
from typing import Optional

_SQRT_2PI = math.sqrt(2 * math.pi)


class GreeksError(Exception):
    """Raised when IV/delta cannot be solved for a given premium. Callers must
    handle this explicitly (usually: fall back to a fixed-points wing rule)."""


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, r: float,
             option_type: str) -> float:
    """European option price via Black-Scholes. option_type: 'CE' or 'PE'.
    Indian index options are European-style, so this is a reasonable model
    (no early-exercise premium to account for)."""
    if t_years <= 0 or sigma <= 0:
        # At/after expiry or degenerate vol -> intrinsic value only.
        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        return intrinsic

    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)

    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    elif option_type == "PE":
        return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    else:
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")


def bs_delta(spot: float, strike: float, t_years: float, sigma: float, r: float,
             option_type: str) -> float:
    """Returns delta as a positive fraction 0..1 for both CE and PE (i.e. this is
    the MAGNITUDE of delta, matching how the PDF/config talks about '25-delta' for
    puts too — not the signed -0.25 a textbook would show for a put)."""
    if t_years <= 0 or sigma <= 0:
        if option_type == "CE":
            return 1.0 if spot > strike else 0.0
        else:
            return 1.0 if spot < strike else 0.0

    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    if option_type == "CE":
        return _norm_cdf(d1)
    elif option_type == "PE":
        return _norm_cdf(d1) - 1.0  # negative; magnitude taken by caller
    else:
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")


def implied_volatility(premium: float, spot: float, strike: float, t_years: float,
                        r: float, option_type: str,
                        lo: float = 0.001, hi: float = 5.0, tol: float = 1e-4,
                        max_iter: int = 100) -> float:
    """Bisection solve for sigma such that bs_price(...) == premium.
    Raises GreeksError if premium is outside the no-arbitrage bounds for [lo, hi]
    sigma, or if the premium itself is non-positive (illiquid/stale quote)."""
    if premium is None or premium <= 0 or t_years <= 0:
        raise GreeksError(f"Cannot solve IV: premium={premium}, t_years={t_years}")

    price_lo = bs_price(spot, strike, t_years, lo, r, option_type)
    price_hi = bs_price(spot, strike, t_years, hi, r, option_type)
    if not (price_lo - premium) * (price_hi - premium) <= 0:
        raise GreeksError(
            f"Premium {premium:.4f} outside solvable IV range "
            f"[{price_lo:.4f}, {price_hi:.4f}] for strike={strike} spot={spot}"
        )

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        price_mid = bs_price(spot, strike, t_years, mid, r, option_type)
        if abs(price_mid - premium) < tol:
            return mid
        if (price_mid - premium) * (price_lo - premium) <= 0:
            hi = mid
        else:
            lo = mid
            price_lo = price_mid
    return (lo + hi) / 2.0


def delta_from_premium(premium: float, spot: float, strike: float, t_years: float,
                        r: float, option_type: str) -> float:
    """Convenience: solve IV then return |delta|. Raises GreeksError on failure —
    callers (strike_selector.py) are expected to catch this and fall back."""
    sigma = implied_volatility(premium, spot, strike, t_years, r, option_type)
    return abs(bs_delta(spot, strike, t_years, sigma, r, option_type))


def expected_move(spot: float, sigma: float, dte_days: float) -> float:
    """EM = S0 * sigma * sqrt(T/365) — one standard deviation move by expiry.
    Used by the (untouched) ATM Iron Condor's Expected-Move wing sizing if you
    choose to switch it over from the fixed OTM7-strikes rule later."""
    return spot * sigma * math.sqrt(dte_days / 365.0)


@dataclass
class VixFallback:
    """Local fallback IV source, per the PDF's 'Dual-Source Data Architecture'.
    Reads a simple CSV of (date, vix_close) rather than a parquet file, since this
    project has no parquet dependency installed — same data, simpler format. Point
    VIX_CSV_PATH (config.py) at a file with columns: date,close (india_vix daily
    close, e.g. exported once from NSE/your broker). This is ONLY used when the
    live Black-Scholes solve fails (illiquid/stale quote) — never as a silent
    substitute for real premium data."""
    csv_path: Optional[str] = None

    def latest_vix(self) -> float:
        import os
        import pandas as pd
        if not self.csv_path or not os.path.isfile(self.csv_path):
            raise GreeksError(
                f"No local VIX fallback file at {self.csv_path!r} — cannot approximate "
                f"IV. Provide VIX_CSV_PATH in .env with columns date,close, or fix the "
                f"live quote issue that triggered this fallback."
            )
        df = pd.read_csv(self.csv_path)
        if df.empty or "close" not in df.columns:
            raise GreeksError(f"VIX fallback file {self.csv_path!r} is empty or malformed")
        df = df.sort_values(df.columns[0])
        return float(df.iloc[-1]["close"]) / 100.0  # VIX quoted in %, sigma as decimal
