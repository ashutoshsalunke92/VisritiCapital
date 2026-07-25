"""
CLI: python run_backtest.py --from 2025-06-01 --to 2025-06-30 --capital 200000

CHANGE FROM ORIGINAL: if params.min_credit_to_width_pct > 0 (set via
MIN_CREDIT_TO_WIDTH_PCT in .env), days where the credit collected is too
small relative to the OTM7 wing width are skipped and reported separately
-- see TUNING_GUIDE.md before enabling this filter.

BUGFIX: previously this crashed at the very end with
  OSError: Cannot save file into a non-existent directory: 'output'
because nothing ever created the output/ folder before to_csv() ran. Now
creates os.path.dirname(args.out) up front, same as param_sweep.py already did.
"""
import argparse
import os
from datetime import datetime, timedelta
import pandas as pd

from config import load_upstox_creds, load_strategy_params
from upstox_client import UpstoxClient
from data_fetcher import fetch_day_data
from backtest_engine import run_day
from pnl_format import print_period_line, print_skip_line, print_summary


def daterange(start: datetime, end: datetime):
    d = start
    while d <= end:
        if d.weekday() < 5:  # skip Sat/Sun; holiday calendar not handled here
            yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    ap.add_argument("--capital", type=float, default=200000.0)
    ap.add_argument("--out", default="output/trades.csv")
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

    daily_results = []
    leg_records = []
    skipped_low_credit = 0

    for d in daterange(start, end):
        try:
            df = fetch_day_data(client, params, d)
            actual_qty = df.attrs.get("lot_size", params.lot_size) * params.lots
            result = run_day(df, params, d, quantity_override=actual_qty)
        except Exception as e:
            print_skip_line(str(d.date()), str(e))
            continue

        if result["status"] != "OK":
            print_skip_line(str(d.date()), result["status"], tag=result["status"])
            continue

        if params.min_credit_to_width_pct > 0:
            credit_ratio = result["net_credit"] / (wing_width_pts * actual_qty)
            if credit_ratio < params.min_credit_to_width_pct:
                print_skip_line(str(d.date()),
                                 f"credit_ratio={credit_ratio:.1%} < threshold "
                                 f"{params.min_credit_to_width_pct:.1%}",
                                 tag="LOW_CREDIT")
                skipped_low_credit += 1
                continue

        daily_results.append({
            "date": result["date"],
            "net_credit": result["net_credit"],
            "close_reason": result["close_reason"],
            "total_pnl": result["total_pnl"],
        })
        leg_records.extend(result["legs"])
        print_period_line(str(d.date()), result["total_pnl"], result["close_reason"])

    if not daily_results:
        print("No trading days produced results. Nothing to summarize.")
        return

    daily_df = pd.DataFrame(daily_results)
    legs_df = pd.DataFrame(leg_records)

    daily_df.to_csv(args.out, index=False)
    legs_df.to_csv(args.out.replace(".csv", "_legs.csv"), index=False)

    total_pnl = daily_df["total_pnl"].sum()
    win_days = (daily_df["total_pnl"] > 0).sum()
    loss_days = (daily_df["total_pnl"] <= 0).sum()
    win_rate = win_days / len(daily_df) * 100

    equity = daily_df["total_pnl"].cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_dd = drawdown.min()

    print_summary(
        title="BACKTEST SUMMARY",
        rows=daily_df, total_pnl=total_pnl, max_dd=max_dd, win_rate=win_rate,
        avg_pnl=daily_df["total_pnl"].mean(), n_win=win_days, n_loss=loss_days,
        capital=args.capital, close_reason_counts=daily_df["close_reason"].value_counts(),
        skipped_low_credit=skipped_low_credit if params.min_credit_to_width_pct > 0 else 0,
        period_word="days",
    )
    print(f"\nDetailed logs written to {args.out} and {args.out.replace('.csv', '_legs.csv')}")


if __name__ == "__main__":
    main()
