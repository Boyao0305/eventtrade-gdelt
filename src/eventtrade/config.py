"""Validated configuration. Built once by the entry point and passed down; nothing deeper reads env vars."""

from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

# Pre-registered split (docs/adr/0001). Changing these is a new ADR, never an edit.
DEV_START = date(2015, 3, 2)
HOLDOUT_START = date(2025, 4, 1)


class Config(BaseModel):
    home: Path = Field(description="Project root holding data/ and secrets/ (the main checkout, not a worktree)")
    gcp_key_path: Path
    gcp_project: str = "ventong-487205"

    # BigQuery cost guards. Free tier is 1 TB/month; leave headroom.
    bq_max_bytes_per_query: int = 50 * 10**9
    bq_monthly_budget_bytes: int = 900 * 10**9

    start: date = DEV_START
    holdout_start: date = HOLDOUT_START
    end: date | None = None

    # Decision clock: GDELT rows added after this New York time belong to the next session.
    cutoff_et: str = "15:30"

    # Pre-registered label horizons in trading days (ADR 0001).
    horizons: tuple[int, ...] = (1, 5, 20)
    # Seed for every random step (LightGBM bagging, stationary bootstrap).
    seed: int = 20261004

    # Costs, one-way, in basis points of traded notional.
    cost_bps_etf: float = 5.0
    cost_bps_stock: float = 10.0
    borrow_bps_annual: float = 50.0

    @property
    def db_path(self) -> Path:
        return self.home / "data" / "eventtrade.sqlite"

    @property
    def intraday_db_path(self) -> Path:
        """ADR 0007: separate store for intraday bars and 15-minute GDELT."""
        return self.home / "data" / "intraday.sqlite"

    @property
    def sec_db_path(self) -> Path:
        """ADR 0011: SEC filings store."""
        return self.home / "data" / "sec.sqlite"

    # SEC requires a User-Agent with a contact (the user's email, approved 2026-10-10).
    sec_user_agent: str = "eventtrade research boyao0305@gmail.com"

    @property
    def alpaca_env_path(self) -> Path:
        return self.home / "secrets" / "alpaca.env"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"
