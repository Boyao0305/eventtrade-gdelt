"""eventtrade CLI entry point: parse arguments into a Config, build adapters, call one pipeline.

This is the composition root: the only module that names concrete adapters. It reads the
environment (EVENTTRADE_HOME) and nothing deeper does. The CLI surface is documented in README.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from eventtrade.cli import pipelines
from eventtrade.config import Config

_DATASETS = ("events", "gkg-themes", "gkg-orgs")
_MODELS = ("ridge", "logistic", "lgbm")
# Hard ceiling for `ingest-gdelt --budget-gb`: the user's $20 spending cap (CLAUDE.md) on top of
# the free 1 TiB/month, at $6.25/TiB, is 1 TiB + 3.2 TiB = 4,617 GB; rounded down.
BQ_BUDGET_CEILING_GB = 4600


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""
    args = _parser().parse_args(argv)
    try:
        cfg = _config(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return pipelines.REFUSED
    from eventtrade.adapters.store import SqliteStore

    store = SqliteStore(cfg.db_path)
    now = _now
    cmd = args.command
    if cmd == "init-db":
        return pipelines.init_db(cfg, store)
    if not cfg.db_path.exists():
        print(f"no store at {cfg.db_path}: run `eventtrade init-db`", file=sys.stderr)
        return pipelines.MISSING
    if cmd == "status":
        return pipelines.status(cfg, store, now)
    if cmd == "load-universe":
        from eventtrade.adapters import universe

        return pipelines.load_universe(cfg, store, args.track, universe.load_etf_universe,
                                       universe.fetch_index_members)
    if cmd == "ingest-prices":
        from eventtrade.adapters import prices

        return pipelines.ingest_prices(cfg, store, _fetch_yf, args.tickers, args.track,
                                       args.start, args.end, prices.sanity_check)
    if cmd == "ingest-gdelt":
        from eventtrade.adapters.gdelt_bq import BudgetExceeded

        if args.budget_gb is not None:
            cfg = cfg.model_copy(update={"bq_monthly_budget_bytes": args.budget_gb * 10**9})

        return pipelines.ingest_gdelt(cfg, store, _gdelt(cfg, store), args.dataset, args.start,
                                      args.end, args.execute, False, now,
                                      BudgetExceeded)
    if cmd == "build-dataset":
        return pipelines.build_dataset(cfg, store, args.track)
    if cmd == "signal-study":
        return pipelines.signal_study(cfg, store, args.track, now)
    if cmd == "screen":
        return pipelines.screen(cfg, store, args.track, now, n_boot=args.n_boot,
                                screen_set=args.screen_set)
    if cmd == "event-study":
        return pipelines.event_study(cfg, store, args.track, now, n_boot=args.n_boot)
    if cmd == "followup":
        return pipelines.followup_f1(cfg, store, now)  # --id F1 only; n_boot fixed at 2000
    if cmd == "backtest":
        return pipelines.backtest(cfg, store, args.track, now, feature=args.feature,
                                  sign=args.sign, model=args.model, model_features=args.features,
                                  horizon=args.horizon, rebalance=args.rebalance)
    if cmd == "overfit-report":
        return pipelines.overfit_report(cfg, store, args.track)
    if cmd == "holdout":
        from eventtrade.adapters.gdelt_bq import BudgetExceeded

        gdelt = _gdelt(cfg, store) if args.i_understand_this_is_final else None
        return pipelines.holdout(cfg, store, args.track, args.trial_id,
                                 args.i_understand_this_is_final, gdelt, _fetch_yf,
                                 args.execute, now, BudgetExceeded)
    raise AssertionError(f"unhandled command {cmd!r}")  # argparse rejects unknown commands


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eventtrade", description=__doc__.splitlines()[0])
    p.add_argument("--home", type=Path, default=None,
                   help="project home holding data/ and secrets/ (default: $EVENTTRADE_HOME)")
    p.add_argument("--gcp-key", type=Path, default=None,
                   help="service-account JSON (default: the single *.json in <home>/secrets)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="row counts, date ranges, BigQuery spend, trial counts")
    sub.add_parser("init-db", help="create the SQLite store (idempotent)")

    s = sub.add_parser("load-universe", help="write Track A ETFs and/or Track B stocks to assets")
    s.add_argument("--track", choices=("A", "B"), default=None, help="default: both")

    s = sub.add_parser("ingest-prices", help="fetch daily bars (yfinance) for the dev period")
    s.add_argument("--start", type=_date, default=None, help="default: config start 2015-03-02")
    s.add_argument("--end", type=_date, default=None,
                   help="inclusive; default 2025-03-31; must be before the hold-out")
    s.add_argument("--tickers", nargs="+", default=None, help="default: all stored assets")
    s.add_argument("--track", choices=("A", "B"), default=None,
                   help="limit default tickers to one track")

    s = sub.add_parser("ingest-gdelt", help="dry-run (default) or pull GDELT month by month")
    s.add_argument("dataset", choices=_DATASETS)
    s.add_argument("--start", type=_date, default=None, help="first session date")
    s.add_argument("--end", type=_date, default=None,
                   help="last session date, inclusive; must be before the hold-out")
    s.add_argument("--execute", action="store_true",
                   help="actually run the queries (default: dry run, estimate only)")
    s.add_argument("--budget-gb", type=_budget_gb, default=None, dest="budget_gb",
                   help="month-to-date BigQuery budget for this run, in GB (default: config 900;"
                        f" at most {BQ_BUDGET_CEILING_GB}, the $20 cap over the free tier)")

    s = sub.add_parser("build-dataset", help="features + labels for one track, dev period")
    s.add_argument("--track", choices=("A", "B"), required=True)

    s = sub.add_parser("signal-study", help="evaluate every feature x horizon, log trials, gate 1")
    s.add_argument("--track", choices=("A", "B"), required=True)

    s = sub.add_parser("screen", help="pre-registered screen of one track (ADR 0003 sub-category"
                                      " screen, or ADR 0004 theme screen); logs trials")
    s.add_argument("--track", choices=("A", "B"), required=True)
    s.add_argument("--set", choices=pipelines.SCREEN_SETS, default="s3", dest="screen_set",
                   help="cell set: s3 = ADR 0003 sub-category screen (default); a2 = ADR 0004"
                        " GKG theme screen, Track A only, needs every dev month of gkg-themes")
    s.add_argument("--n-boot", type=_screen_n_boot, default=2000, dest="n_boot",
                   help="stationary-bootstrap draws for the joint test (2000; at least"
                        f" {pipelines.SCREEN_MIN_N_BOOT}; part of each trial's identity)")

    s = sub.add_parser("event-study", help="pre-registered extreme-shock event study of one"
                                           " track (ADR 0005); logs trials")
    s.add_argument("--track", choices=("A", "B"), required=True)
    s.add_argument("--n-boot", type=_event_n_boot, default=2000, dest="n_boot",
                   help="placebo-date draws for each cell's max-t p and each confirmation p"
                        f" (2000; at least {pipelines.EVENT_MIN_N_BOOT}; part of each trial's"
                        " identity)")

    s = sub.add_parser("followup", help="one pre-registered follow-up test (ADR 0006 F1: EM"
                                        " conflict shocks -> abnormal volatility); logs one trial")
    # No --n-boot: ADR 0006 fixes 2,000 draws, and one F1 trial is ever logged.
    s.add_argument("--id", choices=pipelines.FOLLOWUP_IDS, required=True, dest="followup_id")

    s = sub.add_parser("backtest", help="backtest a rule feature or a model; logs a trial")
    s.add_argument("--track", choices=("A", "B"), required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--feature", default=None, help="rule backtest of one stored feature")
    g.add_argument("--model", choices=_MODELS, default=None, help="walk-forward model backtest")
    s.add_argument("--sign", type=float, default=None,
                   help="rule direction (default: the pre-registered sign of its hypothesis)")
    s.add_argument("--features", nargs="+", default=None,
                   help="model inputs, used as given (default: every stored feature,"
                        " selected per fold on training-date IC)")
    s.add_argument("--horizon", type=int, default=5, help="label horizon in trading days")
    s.add_argument("--rebalance", type=int, default=None,
                   help="rebalance every N trading days (default: --horizon)")

    s = sub.add_parser("overfit-report", help="DSR, PBO and bootstrap CI over backtest trials")
    s.add_argument("--track", choices=("A", "B"), required=True)

    s = sub.add_parser("holdout", help="FINAL: evaluate one backtest trial once on the hold-out")
    s.add_argument("--track", choices=("A", "B"), required=True)
    s.add_argument("--trial-id", required=True, help="a backtest trial id (rule backtests only)")
    s.add_argument("--execute", action="store_true",
                   help="fetch hold-out prices and GDELT, then evaluate"
                        " (default: dry run, fetches and writes nothing)")
    s.add_argument("--i-understand-this-is-final", action="store_true",
                   dest="i_understand_this_is_final")
    return p


def _config(args: argparse.Namespace) -> Config:
    home = args.home or os.environ.get("EVENTTRADE_HOME")
    if not home:
        raise ValueError("set EVENTTRADE_HOME or pass --home")
    home = Path(home)
    key = args.gcp_key
    if key is None:
        found = sorted((home / "secrets").glob("*.json"))
        key = found[0] if len(found) == 1 else home / "secrets" / "gcp-key.json"
    return Config(home=home, gcp_key_path=key)


def _gdelt(cfg: Config, store: Any) -> Any:
    from google.cloud import bigquery

    from eventtrade.adapters.gdelt_bq import GdeltBigQuery

    if not cfg.gcp_key_path.is_file():
        raise SystemExit(f"no GCP key at {cfg.gcp_key_path}: pass --gcp-key")
    client = bigquery.Client.from_service_account_json(str(cfg.gcp_key_path),
                                                       project=cfg.gcp_project)
    return GdeltBigQuery(client, store, cfg.bq_max_bytes_per_query, cfg.bq_monthly_budget_bytes,
                         _now)


def _fetch_yf(tickers: list[str], start: date, end: date) -> Any:
    from eventtrade.adapters.prices import fetch_yfinance

    return fetch_yfinance(tickers, start, end)


def _now() -> datetime:
    return datetime.now(UTC)


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _budget_gb(s: str) -> int:
    """`--budget-gb`: a positive whole number of GB, never above the spending-cap ceiling."""
    n = int(s)
    if not 1 <= n <= BQ_BUDGET_CEILING_GB:
        raise argparse.ArgumentTypeError(f"must be 1..{BQ_BUDGET_CEILING_GB}, got {n}")
    return n


def _screen_n_boot(s: str) -> int:
    """ADR 0003 implementation notes: n_boot >= 999; fewer is a usage error (exit 2)."""
    n = int(s)
    if n < pipelines.SCREEN_MIN_N_BOOT:
        raise argparse.ArgumentTypeError(f"must be >= {pipelines.SCREEN_MIN_N_BOOT}, got {n}")
    return n


def _event_n_boot(s: str) -> int:
    """ADR 0005: n_boot >= 999; fewer is a usage error (exit 2)."""
    n = int(s)
    if n < pipelines.EVENT_MIN_N_BOOT:
        raise argparse.ArgumentTypeError(f"must be >= {pipelines.EVENT_MIN_N_BOOT}, got {n}")
    return n


if __name__ == "__main__":
    sys.exit(main())
