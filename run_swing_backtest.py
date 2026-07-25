"""
Swing-trade version: enter Wednesday 10:16 (the day after Tuesday's weekly
expiry), hold through the following Monday 14:59 — unless an SL/TP rule
fires earlier. Same 4 legs, same entry/exit rules as the original strategy,
just no more daily square-off.

    python run_swing_backtest.py --from 2026-01-01 --to 2026-07-15 --capital 400000

CHANGE FROM ORIGINAL: same optional low-credit entry filter as
run_backtest.py — see TUNING_GUIDE.md.

BUGFIX: same missing-output-directory crash as run_backtest.py, fixed the
same way (os.makedirs before to_csv).
"""
import argparse
import os
from datetime import datetime, timedelta
import pandas as pd

from config import load_upstox_creds, load_strategy_params
from upstox_client import UpstoxClient
from data_fetcher import fetch_swing_trade_data
from backtest_engine import run_swing_trade
from pnl_format import print_period_line, print_skip_line, print_summary


def wednesdays_in_range(start: datetime, end: datetime):
    d = start
    while d.weekday() != 2:  # 0=Mon .. 2=Wed
        d += timedelta(days=1)
    while d <= end:
        yield d
        d += timedelta(days=7)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    ap.add_argument("--capital", type=float, default=200000.0)
    ap.add_argument("--out", default="output/swing_trades.csv")
    args = ap.parse_args()

    creds = load_upstox_creds()
    params = load_strategy_params()
    client = UpstoxClient(creds)

    # BUGFIX: create the output folder before anything tries to write into it.
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    start = datetime.strptime(args.date_from, "%Y-%m-%d")
    end = datetime.strptime(args.date_to, "%Y-%m-%d")

    wing_width_pts = params.otm_wing_strikes * params.strike_step

    weekly_results = []
    leg_records = []
    skipped_low_credit = 0

    for entry_date in wednesdays_in_range(start, end):
        exit_date = entry_date + timedelta(days=5)  # Wed -> following Monday
        try:
            df = fetch_swing_trade_data(client, params, entry_date, exit_date)
            entry_dt = df.attrs["entry_dt"]
            exit_dt = df.attrs["exit_dt"]
            actual_qty = df.attrs.get("lot_size", params.lot_size) * params.lots
            result = run_swing_trade(df, params, entry_dt, exit_dt, quantity_override=actual_qty)
        except Exception as e:
            print_skip_line(f"{entry_date.date()}->{exit_date.date()}", str(e))
            continue

        if result["status"] != "OK":
            print_skip_line(f"{entry_date.date()}->{exit_date.date()}",
                             result["status"], tag=result["status"])
            continue

        if params.min_credit_to_width_pct > 0:
            credit_ratio = result["net_credit"] / (wing_width_pts * actual_qty)
            if credit_ratio < params.min_credit_to_width_pct:
                print_skip_line(f"{entry_date.date()}->{exit_date.date()}",
                                 f"credit_ratio={credit_ratio:.1%} < threshold "
                                 f"{params.min_credit_to_width_pct:.1%}",
                                 tag="LOW_CREDIT")
                skipped_low_credit += 1
                continue

        weekly_results.append({
            "entry_date": result["entry_date"],
            "planned_exit_date": result["planned_exit_date"],
            "net_credit": result["net_credit"],
            "close_reason": result["close_reason"],
            "total_pnl": result["total_pnl"],
        })
        leg_records.extend(result["legs"])
        print_period_line(f"{entry_date.date()}->{exit_date.date()}",
                           result["total_pnl"], result["close_reason"])

    if not weekly_results:
        print("No swing trades produced results. Nothing to summarize.")
        return

    weekly_df = pd.DataFrame(weekly_results)
    legs_df = pd.DataFrame(leg_records)

    weekly_df.to_csv(args.out, index=False)
    legs_df.to_csv(args.out.replace(".csv", "_legs.csv"), index=False)

    total_pnl = weekly_df["total_pnl"].sum()
    win_weeks = (weekly_df["total_pnl"] > 0).sum()
    loss_weeks = (weekly_df["total_pnl"] <= 0).sum()
    win_rate = win_weeks / len(weekly_df) * 100

    equity = weekly_df["total_pnl"].cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_dd = drawdown.min()

    print_summary(
        title="SWING BACKTEST SUMMARY",
        rows=weekly_df, total_pnl=total_pnl, max_dd=max_dd, win_rate=win_rate,
        avg_pnl=weekly_df["total_pnl"].mean(), n_win=win_weeks, n_loss=loss_weeks,
        capital=args.capital, close_reason_counts=weekly_df["close_reason"].value_counts(),
        skipped_low_credit=skipped_low_credit if params.min_credit_to_width_pct > 0 else 0,
        period_word="weeks",
    )
    print(f"\nDetailed logs written to {args.out} and {args.out.replace('.csv', '_legs.csv')}")


if __name__ == "__main__":
    main()
