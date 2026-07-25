"""
Central config — backward-compatible with the original Iron Fly .env, extended
for the Hedged 25-Delta Strangle and this round's fixes:

  - BACKTEST_WINDOW_DAYS: fixed lookback window (default 180) used automatically
    for backtest/forward-test so the menu never has to ask for dates. Change it
    here (or override with --from/--to on the individual CLI scripts) — a proper
    "change window" menu option is a later addition, not built yet on purpose.
  - SL_MODE / TRAIL_ACTIVATE_PCT / TRAIL_GIVEBACK_PCT: switches BOTH strategies'
    combined exit from a static SL/TP to a trailing SL. See strategy.py /
    strangle_strategy.py docstrings for the exact mechanics.
  - MONTHLY_STRATEGY_SL_PCT / MONTHLY_STRATEGY_TP_PCT: the Iron Condor's monthly
    runner is now a genuinely different exit window from the weekly runner (see
    TUNING_GUIDE_V2.md) — these default to 2x the weekly thresholds if unset,
    matching the PDF's 4%/2% (weekly) -> 8%/4% (monthly) ratio.
  - IRON_CONDOR_MONTHLY_EXIT_DAYS_BEFORE_EXPIRY: monthly runner exits this many
    calendar days before that cycle's monthly expiry (PDF: 2 weeks).
  - STRANGLE_STRIKE_WINDOW: bounds how many strikes on each side of ATM the
    strangle chain-fetcher pulls candles for. Previously unbounded (pulled the
    ENTIRE contract list every replay tick) -- this was the main reason the
    strangle looked "broken" (rate-limited / effectively never finished).
  - Both runners' capital is FIXED at ₹250,000 (STRANGLE_CAPITAL /
    IRONFLY_CAPITAL below) — the menu no longer asks for it.
"""
import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_THIS_DIR, ".env")
load_dotenv(_ENV_PATH)


def _get(key: str, default=None, cast=str):
    val = os.getenv(key, default)
    if val is None:
        return None
    return cast(val)


def _get_optional_float(key: str, default=None):
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return float(val)


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


# =========================================================================
# ORIGINAL — unchanged
# =========================================================================

@dataclass
class UpstoxCreds:
    access_token: str


@dataclass
class StrategyParams:
    """Iron Fly / ATM Iron Condor."""
    entry_time: str
    exit_time: str
    per_leg_sl_pct: float
    strategy_sl_pct: float
    strategy_tp_pct: float
    lot_size: int
    lots: int
    strike_step: int
    otm_wing_strikes: int
    underlying: str
    hedge_leg_sl_pct: Optional[float] = None
    min_credit_to_width_pct: float = 0.0
    # --- NEW: fixed capital (menu no longer prompts for it) ---
    capital_deployed: float = 250000.0
    # --- NEW: trailing SL (see strategy.py IronFlyState.on_tick) ---
    sl_mode: str = "STATIC"                # "STATIC" | "TRAILING"
    trail_activate_pct: float = 0.5        # fraction of TP reached before trailing arms
    trail_giveback_pct: float = 0.3        # fraction of peak profit allowed to give back
    # --- NEW: monthly runner gets its own thresholds (see TUNING_GUIDE_V2.md #6) ---
    monthly_strategy_sl_pct: Optional[float] = None   # defaults to 2x strategy_sl_pct
    monthly_strategy_tp_pct: Optional[float] = None   # defaults to 2x strategy_tp_pct
    monthly_exit_days_before_expiry: int = 14

    @property
    def quantity(self) -> int:
        return self.lot_size * self.lots

    def resolved_monthly_sl_pct(self) -> float:
        return self.monthly_strategy_sl_pct if self.monthly_strategy_sl_pct is not None else self.strategy_sl_pct * 2

    def resolved_monthly_tp_pct(self) -> float:
        return self.monthly_strategy_tp_pct if self.monthly_strategy_tp_pct is not None else self.strategy_tp_pct * 2


def load_upstox_creds() -> UpstoxCreds:
    access_token = _get("UPSTOX_ACCESS_TOKEN")
    if not access_token:
        env_exists = os.path.isfile(_ENV_PATH)
        raise RuntimeError(
            "UPSTOX_ACCESS_TOKEN not set.\n"
            f"  Looked for .env at: {_ENV_PATH}\n"
            f"  That file {'EXISTS' if env_exists else 'DOES NOT EXIST'}.\n"
            "  Run: python check_setup.py"
        )
    return UpstoxCreds(access_token=access_token)


def load_strategy_params() -> StrategyParams:
    return StrategyParams(
        entry_time=_get("ENTRY_TIME", "10:16"),
        exit_time=_get("EXIT_TIME", "14:59"),
        per_leg_sl_pct=_get("PER_LEG_SL_PCT", "0.25", float),
        strategy_sl_pct=_get("STRATEGY_SL_PCT", "0.25", float),
        strategy_tp_pct=_get("STRATEGY_TP_PCT", "0.25", float),
        lot_size=_get("LOT_SIZE", "75", int),
        lots=_get("LOTS", "1", int),
        strike_step=_get("STRIKE_STEP", "50", int),
        otm_wing_strikes=_get("OTM_WING_STRIKES", "7", int),
        underlying=_get("UNDERLYING", "NIFTY"),
        hedge_leg_sl_pct=_get_optional_float("HEDGE_LEG_SL_PCT", None),
        min_credit_to_width_pct=_get("MIN_CREDIT_TO_WIDTH_PCT", "0.0", float),
        capital_deployed=_get("IRONFLY_CAPITAL", "250000", float),
        sl_mode=_get("SL_MODE", "STATIC").upper(),
        trail_activate_pct=_get("TRAIL_ACTIVATE_PCT", "0.5", float),
        trail_giveback_pct=_get("TRAIL_GIVEBACK_PCT", "0.3", float),
        monthly_strategy_sl_pct=_get_optional_float("MONTHLY_STRATEGY_SL_PCT", None),
        monthly_strategy_tp_pct=_get_optional_float("MONTHLY_STRATEGY_TP_PCT", None),
        monthly_exit_days_before_expiry=_get("IRON_CONDOR_MONTHLY_EXIT_DAYS_BEFORE_EXPIRY", "14", int),
    )


# =========================================================================
# Hedged 25-Delta Strangle
# =========================================================================

class StrangleTimeframe:
    INTRADAY = "INTRADAY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


_TIMEFRAME_RATIOS = {
    StrangleTimeframe.INTRADAY: (0.02, 0.01),
    StrangleTimeframe.WEEKLY: (0.04, 0.02),
    StrangleTimeframe.MONTHLY: (0.08, 0.04),
}


@dataclass
class StrangleParams:
    underlying: str
    entry_time: str
    exit_time: str
    timeframe: str
    lot_size: int
    lots: int
    strike_step: int
    capital_deployed: float
    short_leg_target_delta: float = 25.0
    delta_tolerance: float = 3.0
    hedge_target_delta: float = 5.0
    hedge_fallback_points: float = 350.0
    risk_free_rate: float = 0.065
    adjustment_delta_breach: float = 40.0
    adjustment_max_per_trade: int = 2
    adjustment_no_roll_inside_dte: int = 10
    adjustment_max_debit_pct_capital: float = 0.005
    min_ivr_pct: float = 0.0
    # --- NEW: bounded strike window + on-disk day-series caching (fixes the
    # "fetches the whole chain every 5 minutes" performance/rate-limit bug) ---
    strike_window: int = 20
    replay_interval_mins: int = 5
    # --- NEW: trailing SL, same mechanics as the Iron Fly's ---
    sl_mode: str = "STATIC"
    trail_activate_pct: float = 0.5
    trail_giveback_pct: float = 0.3

    @property
    def quantity(self) -> int:
        return self.lot_size * self.lots

    @property
    def tp_capital_pct(self) -> float:
        return _TIMEFRAME_RATIOS[self.timeframe][0]

    @property
    def sl_capital_pct(self) -> float:
        return _TIMEFRAME_RATIOS[self.timeframe][1]

    @property
    def adjustment_enabled(self) -> bool:
        return self.timeframe == StrangleTimeframe.MONTHLY


def load_strangle_params() -> StrangleParams:
    return StrangleParams(
        underlying=_get("STRANGLE_UNDERLYING", "NIFTY"),
        entry_time=_get("STRANGLE_ENTRY_TIME", "10:16"),
        exit_time=_get("STRANGLE_EXIT_TIME", "14:59"),
        timeframe=_get("STRANGLE_TIMEFRAME", StrangleTimeframe.INTRADAY),
        lot_size=_get("LOT_SIZE", "75", int),
        lots=_get("STRANGLE_LOTS", "1", int),
        strike_step=_get("STRIKE_STEP", "50", int),
        capital_deployed=_get("STRANGLE_CAPITAL", "250000", float),
        short_leg_target_delta=_get("STRANGLE_SHORT_DELTA", "25.0", float),
        delta_tolerance=_get("STRANGLE_DELTA_TOLERANCE", "3.0", float),
        hedge_target_delta=_get("STRANGLE_HEDGE_DELTA", "5.0", float),
        hedge_fallback_points=_get("STRANGLE_HEDGE_FALLBACK_PTS", "350.0", float),
        risk_free_rate=_get("RISK_FREE_RATE", "0.065", float),
        adjustment_delta_breach=_get("ADJ_DELTA_BREACH", "40.0", float),
        adjustment_max_per_trade=_get("ADJ_MAX_PER_TRADE", "2", int),
        adjustment_no_roll_inside_dte=_get("ADJ_NO_ROLL_DTE", "10", int),
        adjustment_max_debit_pct_capital=_get("ADJ_MAX_DEBIT_PCT", "0.005", float),
        min_ivr_pct=_get("STRANGLE_MIN_IVR_PCT", "0.0", float),
        strike_window=_get("STRANGLE_STRIKE_WINDOW", "20", int),
        replay_interval_mins=_get("REPLAY_INTERVAL_MINS", "5", int),
        sl_mode=_get("SL_MODE", "STATIC").upper(),
        trail_activate_pct=_get("TRAIL_ACTIVATE_PCT", "0.5", float),
        trail_giveback_pct=_get("TRAIL_GIVEBACK_PCT", "0.3", float),
    )


# =========================================================================
# Notifications / external APIs
# =========================================================================

@dataclass
class NotificationConfig:
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    finnhub_api_key: Optional[str]
    vix_csv_path: Optional[str]


def load_notification_config() -> NotificationConfig:
    return NotificationConfig(
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
        finnhub_api_key=_get("FINNHUB_API_KEY"),
        vix_csv_path=_get("VIX_CSV_PATH"),
    )


# =========================================================================
# NEW — Session defaults (kills the per-run prompts)
# =========================================================================

@dataclass
class SessionDefaults:
    backtest_window_days: int
    output_dir: str
    fixed_capital: float


def load_session_defaults() -> SessionDefaults:
    return SessionDefaults(
        backtest_window_days=_get("BACKTEST_WINDOW_DAYS", "180", int),
        output_dir=_get("OUTPUT_DIR", "output"),
        fixed_capital=_get("FIXED_CAPITAL", "250000", float),
    )
