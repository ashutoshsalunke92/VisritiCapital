"""
Shared console formatting for backtest P&L output — used by both
run_backtest.py and run_swing_backtest.py so the two CLIs look consistent.

Color rule: green = profit, red = loss, dim yellow = breakeven (exactly 0).
Falls back to plain (uncolored) text automatically if colorama isn't
installed or the terminal doesn't support ANSI — nothing breaks either way.
"""
try:
    from colorama import init as _colorama_init, Fore, Style
    _colorama_init(autoreset=True)
    _COLOR = True
except ImportError:
    _COLOR = False

    class _NoColor:
        def __getattr__(self, _):
            return ""
    Fore = Style = _NoColor()


def _pnl_color(value: float) -> str:
    if value > 0:
        return Fore.GREEN
    if value < 0:
        return Fore.RED
    return Fore.YELLOW


def fmt_pnl(value: float, width: int = 12) -> str:
    """Right-aligned, thousands-separated, sign-prefixed, colored P&L string."""
    text = f"{value:>+,.2f}"
    return f"{_pnl_color(value)}{text:>{width}}{Style.RESET_ALL}"


def fmt_pct(value: float, width: int = 8) -> str:
    text = f"{value:>+.2f}%"
    return f"{_pnl_color(value)}{text:>{width}}{Style.RESET_ALL}"


def print_period_line(label: str, pnl: float, reason: str, extra: str = ""):
    """One line per traded day/week: label left-aligned, pnl right-aligned
    and colored, reason left-aligned in its own column."""
    reason_col = f"{reason:<14}"
    line = f"[{label:<12}]  P&L: {fmt_pnl(pnl)}   {reason_col}"
    if extra:
        line += f"  {extra}"
    print(line)


def print_skip_line(label: str, message: str, tag: str = "SKIPPED"):
    color = Fore.YELLOW if _COLOR else ""
    reset = Style.RESET_ALL if _COLOR else ""
    print(f"[{label:<12}]  {color}{tag:<9}{reset} {message}")


def print_summary(title: str, rows: list, total_pnl: float, max_dd: float,
                   win_rate: float, avg_pnl: float, n_win: int, n_loss: int,
                   capital: float, close_reason_counts, skipped_low_credit: int = 0,
                   period_word: str = "days"):
    bar = "=" * 60
    bold = Style.BRIGHT if _COLOR else ""
    reset = Style.RESET_ALL if _COLOR else ""

    print(f"\n{bold}{bar}")
    print(f"{title:^60}")
    print(f"{bar}{reset}")
    print(f"{'Total ' + period_word.capitalize() + ' traded':<28}: {len(rows)}")
    if skipped_low_credit:
        print(f"{'Skipped (low credit)':<28}: {skipped_low_credit}")
    print(f"{'Total P&L':<28}: {fmt_pnl(total_pnl, width=14)}")
    print(f"{'Win / Loss ' + period_word:<28}: {n_win} / {n_loss}   ({win_rate:.1f}% win rate)")
    print(f"{'Avg P&L / ' + period_word[:-1]:<28}: {fmt_pnl(avg_pnl, width=14)}")
    print(f"{'Max drawdown (equity)':<28}: {fmt_pnl(max_dd, width=14)}")
    print(f"{'Return on capital (' + format(capital, ',.0f') + ')':<28}: "
          f"{fmt_pct(total_pnl / capital * 100, width=14)}")
    print(f"\n{bold}Close reason breakdown:{reset}")
    for reason, count in close_reason_counts.items():
        print(f"  {reason:<16}: {count}")
    print(f"{bold}{bar}{reset}")
