"""Command-line entry point for batch jobs — run on demand or by cron, never as a
permanent process. From the project root (or inside the api container):

    python -m app.workers.cli analytics rebuild
    python -m app.workers.cli analytics refresh [--min-days 7]
    python -m app.workers.cli analytics verify
    python -m app.workers.cli forecast run [--model M ...] [--target T ...] [--horizon 14] [--train-end YYYY-MM-DD]

Exit code is non-zero on failure (and on any failed verify check), so cron/CI can tell.
"""

import argparse
import sys
import time
from datetime import date

from app.analytics import jobs
from app.forecasting import service as forecasting
from app.forecasting.models.baseline import MODELS


def _print_result(result: jobs.RunResult, seconds: float) -> None:
    if result.from_date is None:
        print(f"analytics {result.job}: nothing to compute ({seconds:.1f}s)")
        return
    print(
        f"analytics {result.job}: {result.from_date} .. {result.to_date} - "
        f"daily_sales {result.rows_sales} rows, daily_stock_snapshot {result.rows_stock} rows ({seconds:.1f}s)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.cli")
    groups = parser.add_subparsers(dest="group", required=True)

    analytics = groups.add_parser("analytics", help="analytics schema jobs (Fase 7)")
    commands = analytics.add_subparsers(dest="command", required=True)
    commands.add_parser("rebuild", help="drop and recompute every analytics row from public")
    refresh = commands.add_parser("refresh", help="recompute what changed since the last run, up to yesterday")
    refresh.add_argument("--min-days", type=int, default=7, help="always recompute at least the last N days (default 7)")
    commands.add_parser("verify", help="prove the analytics tables are derivable from public")

    forecast = groups.add_parser("forecast", help="forecast generation (Fase 8)")
    forecast_commands = forecast.add_subparsers(dest="command", required=True)
    run = forecast_commands.add_parser("run", help="run the models and persist every forecast")
    run.add_argument("--model", action="append", choices=list(MODELS), help="repeatable; default: all models")
    run.add_argument("--target", action="append", choices=list(forecasting.TARGETS), help="repeatable; default: both")
    run.add_argument("--horizon", type=int, default=forecasting.DEFAULT_HORIZON, help="days ahead (default 14)")
    run.add_argument("--train-end", type=date.fromisoformat, help="last training day (default: last day with a sale)")
    run.add_argument("--filial-id", type=int, action="append", help="repeatable; default: every filial with sales")

    args = parser.parse_args(argv)
    started = time.monotonic()

    if args.group == "forecast":
        summaries = forecasting.generate(args.filial_id, args.model, args.target, args.horizon, args.train_end)
        for r in summaries:
            wape = (r.metrics.get("filial") or {}).get("wape")
            print(
                f"forecast run {r.run_id}: filial {r.filial_id} {r.target:<8} {r.model:<24} "
                f"train {r.train_start}..{r.train_end}, {r.n_series} series, holdout WAPE (filial) {wape}"
            )
        if not summaries:
            print("forecast: nothing to run (no analytics data - run 'analytics rebuild' first)")
        print(f"({time.monotonic() - started:.1f}s)")
        return 0
    if args.command == "rebuild":
        _print_result(jobs.rebuild(), time.monotonic() - started)
    elif args.command == "refresh":
        _print_result(jobs.refresh(min_days=args.min_days), time.monotonic() - started)
    elif args.command == "verify":
        checks = jobs.verify()
        for name, ok, detail in checks:
            print(f"{'OK  ' if ok else 'FAIL'}  {name}  [{detail}]")
        failed = sum(not ok for _, ok, _ in checks)
        print(f"\n{len(checks) - failed}/{len(checks)} checks passed ({time.monotonic() - started:.1f}s)")
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
