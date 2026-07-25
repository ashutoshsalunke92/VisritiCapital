"""
Interactive CLI menu.

    python menu.py

CHANGES IN THIS REVISION (fixes #3/#4/#5):
  - No date prompt, ever. Backtest/forward-test use a fixed rolling window
    (BACKTEST_WINDOW_DAYS in .env, default 180 days back from today) shown
    read-only in the confirmation summary. Change the window by editing
    .env — a menu option to change it in-session is a later addition.
  - No output-directory prompt. Always "output/".
  - No capital prompt. Always ₹250,000 per runner (FIXED_CAPITAL in .env).
  - Forward-test is now clearly labeled as running against the REAL live
    market (paper fills only) -- not a historical replay.
"""
import os
import sys
from datetime import date, timedelta

try:
    from colorama import init as _cinit, Fore, Style
    _cinit(autoreset=True)
    _C = True
except ImportError:
    _C = False
    class _NC:
        def __getattr__(self, _): return ""
    Fore = Style = _NC()

def _h(text):  return f"{Style.BRIGHT}{text}{Style.RESET_ALL}" if _C else text
def _g(text):  return f"{Fore.GREEN}{text}{Style.RESET_ALL}" if _C else text
def _y(text):  return f"{Fore.YELLOW}{text}{Style.RESET_ALL}" if _C else text
def _r(text):  return f"{Fore.RED}{text}{Style.RESET_ALL}" if _C else text
def _c(text):  return f"{Fore.CYAN}{text}{Style.RESET_ALL}" if _C else text


def _banner():
    print()
    print(_h("=" * 62))
    print(_h("  NIFTY OPTIONS ALGO — Strategy Console"))
    print(_h("=" * 62))
    print()


def _menu(title: str, options: list, allow_back=True) -> str:
    print(_c(f"\n── {title} ──"))
    for i, (key, label) in enumerate(options, 1):
        print(f"  {_y(str(i))}.  {label}")
    if allow_back:
        print(f"  {_y('0')}.  ← Back / Exit")
    while True:
        raw = input(_g("\nEnter choice: ")).strip()
        if raw == "0" and allow_back:
            return "__back__"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print(_r("  Invalid choice — please try again."))


def _confirm(summary: dict) -> bool:
    print()
    print(_h("── Session Summary ──"))
    for k, v in summary.items():
        print(f"  {k:<22}: {_g(str(v))}")
    print()
    ans = input(_y("Proceed? (y/N): ")).strip().lower()
    return ans == "y"


# ---------------------------------------------------------------------------
# Sub-flows
# ---------------------------------------------------------------------------

def _flow_backtest(strategy: str) -> dict:
    """FIX #3: no date prompt -- fixed window, read from .env (default 180
    days), shown for information only."""
    from config import load_session_defaults
    sd = load_session_defaults()
    today = date.today()
    date_from = (today - timedelta(days=sd.backtest_window_days)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    print(_c(f"\n  Backtest window is fixed at {sd.backtest_window_days} days: "
             f"{date_from} .. {date_to}"))
    print(_c(f"  (change BACKTEST_WINDOW_DAYS in .env to adjust — not prompted per run)"))

    tf_choice = _menu("Select timeframe", [
        ("intraday", "Intraday (enter, square off same day)"),
        ("weekly",   "Weekly positional (enter Wednesday, exit following Monday)"),
        ("monthly",  "Monthly positional (enter Wednesday, exit 2 weeks before monthly expiry)"),
    ])
    if tf_choice == "__back__":
        return None

    use_cache = True
    if strategy == "iron_fly":
        ans = input(f"  Use local cache for Iron Fly data? ({_y('Y')}/n): ").strip().lower()
        use_cache = ans != "n"

    return dict(strategy=strategy, mode="backtest", timeframe=tf_choice,
                use_cache=use_cache, dry_run=True)


def _flow_forward_test(strategy: str) -> dict:
    """FIX #8: forward-test runs against the REAL live market right now
    (paper fills only) -- it is NOT a date-range replay, so no date prompt
    makes sense here either."""
    print(_c("\n  Forward-test runs against the REAL live market, starting now."))
    print(_c("  Orders are simulated (paper) — nothing is sent to the exchange."))

    tf_options = [("intraday", "Intraday — enter now, square off at configured exit time")]
    if strategy == "strangle":
        tf_options += [
            ("weekly", "Weekly positional — enter now, monitor toward Monday exit"),
            ("monthly", "Monthly positional — enter now, monitor with adjustment engine"),
        ]
    tf_choice = _menu("Select timeframe", tf_options)
    if tf_choice == "__back__":
        return None

    return dict(strategy=strategy, mode="forward_test", timeframe=tf_choice, dry_run=True)


def _flow_live(strategy: str) -> dict:
    tf_options = [("intraday", "Intraday — enter now, square off at configured exit time")]
    if strategy == "strangle":
        tf_options += [
            ("weekly", "Weekly positional"),
            ("monthly", "Monthly positional"),
        ]
    tf_choice = _menu("Select timeframe", tf_options)
    if tf_choice == "__back__":
        return None

    print()
    print(_r("  ⚠  Live trading sends REAL orders via Upstox once DRY_RUN is disabled."))
    print(_r("     Start with DRY_RUN=yes to observe behaviour safely first."))
    dry_str = input(f"  DRY RUN (yes=paper only, no=real orders) [{_y('yes')}]: ").strip().lower() or "yes"
    dry_run = dry_str in ("yes", "y")

    return dict(strategy=strategy, mode="live", timeframe=tf_choice, dry_run=dry_run)


def _flow_resume_paused():
    from event_calendar import PendingConfirmations
    p = PendingConfirmations()
    paused = p.list_paused()
    if not paused:
        print(_g("  No runners are currently paused."))
        return
    print(_c("\n── Paused Runners ──"))
    for i, item in enumerate(paused, 1):
        print(f"  {i}. runner_id={item['runner_id']}  reason={item.get('reason','')}  "
              f"since={item.get('paused_at','')}")
    raw = input("  Enter number to resume (0 to cancel): ").strip()
    if not raw.isdigit() or not (0 <= int(raw) <= len(paused)):
        return
    idx = int(raw)
    if idx == 0:
        return
    runner_id = paused[idx - 1]["runner_id"]
    p.resume_runner(runner_id)
    print(_g(f"  Resumed {runner_id}"))


def _flow_show_upcoming_events():
    try:
        from config import load_notification_config
        from event_calendar import EventCalendar
        nc = load_notification_config()
        cal = EventCalendar(finnhub_api_key=nc.finnhub_api_key)
        events = cal.upcoming_high_impact(window_days=30)
        print(_c("\n── High-Impact Events — Next 30 Days ──"))
        if not events:
            print("  None found in static calendar (Finnhub supplement requires FINNHUB_API_KEY).")
        for e in events:
            print(f"  {e.event_date}  [{_r(e.impact):<6}]  {e.name}  [{e.source}]")
    except Exception as ex:
        print(_r(f"  Error loading calendar: {ex}"))


def _flow_check_setup():
    print(_c("\n── Running check_setup.py ──\n"))
    os.system(f"{sys.executable} check_setup.py")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    _banner()
    if not os.path.isfile(".env"):
        print(_r("  ⚠  No .env file found. Run check_setup.py first."))
        print()

    while True:
        action = _menu("Main Menu", [
            ("run",    "Run a strategy (backtest / forward-test / live)"),
            ("events", "Show upcoming high-impact events"),
            ("resume", "Resume a paused runner"),
            ("setup",  "Check setup / credentials"),
        ])
        if action == "__back__":
            print(_g("\nGoodbye.\n"))
            break

        elif action == "run":
            strat = _menu("Select strategy", [
                ("iron_fly", "ATM Iron Fly / Iron Condor  (existing, unchanged core logic)"),
                ("strangle", "Hedged 25-Delta Strangle"),
            ])
            if strat == "__back__":
                continue

            mode = _menu("Select mode", [
                ("backtest",     "Backtest      — real historical data, fixed lookback window"),
                ("forward_test", "Forward Test  — REAL live market data, paper trade, starts now"),
                ("live",         "Live Trading  — real live market, real orders (unless DRY RUN)"),
            ])
            if mode == "__back__":
                continue

            if mode == "backtest":
                cfg_dict = _flow_backtest(strat)
            elif mode == "forward_test":
                cfg_dict = _flow_forward_test(strat)
            else:
                cfg_dict = _flow_live(strat)

            if cfg_dict is None:
                continue

            summary = {
                "Strategy":   cfg_dict["strategy"],
                "Mode":       cfg_dict["mode"],
                "Timeframe":  cfg_dict["timeframe"],
                "Capital":    "₹250,000 (fixed)",
                "Output dir": "output/ (fixed)",
                "Dry run":    "Yes" if cfg_dict.get("dry_run", True) else _r("NO — REAL ORDERS"),
            }
            if not _confirm(summary):
                print(_y("  Cancelled."))
                continue

            from trading_engine import RunConfig, run_session
            rc = RunConfig(**{k: v for k, v in cfg_dict.items()
                               if k in RunConfig.__dataclass_fields__})
            try:
                run_session(rc)
            except KeyboardInterrupt:
                print(_y("\n  Interrupted by user."))
            except Exception as e:
                print(_r(f"\n  Error: {e}"))
                import traceback; traceback.print_exc()

        elif action == "events":
            _flow_show_upcoming_events()
        elif action == "resume":
            _flow_resume_paused()
        elif action == "setup":
            _flow_check_setup()

        input(_y("\n  Press Enter to return to the main menu..."))


if __name__ == "__main__":
    main()
