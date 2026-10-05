"""Shared vocabulary. Imports nothing from the package; data only.

Tabular data crosses module boundaries as pandas DataFrames in *long* format. The column
contracts below are the schema for those frames — every producer emits exactly these columns,
every consumer may rely on them. Extra columns are a contract change.

Every date column (`session_date`, `date`) is `datetime64[ns]` at midnight, tz-naive.
"""

from datetime import date, datetime
from typing import Literal, NewType

from pydantic import BaseModel, Field

Ticker = NewType("Ticker", str)
EntityId = NewType("EntityId", str)  # GDELT-side key: FIPS country code, theme, or org alias id
TrialId = NewType("TrialId", str)

Track = Literal["A", "B"]  # A: event/theme shocks -> ETFs; B: company news -> stocks

# --- DataFrame column contracts -------------------------------------------------------------

# Raw GDELT aggregates as pulled. `session_date` is the New York calendar date the rows belong
# to after applying the cutoff (rows added after cutoff_et roll to the next calendar day).
# Non-trading calendar days are NOT yet rolled forward — core/features.py does that.
# `value_sum` and `n` are kept separately so re-aggregation over days stays exact.
GDELT_DAILY_COLUMNS = ["session_date", "source", "entity", "metric", "value_sum", "n"]
GdeltSource = Literal["events", "gkg"]

# Daily bars. open/high/low/close/volume are split-adjusted only (Yahoo's raw bars);
# adj_open/adj_close are split- AND dividend-adjusted (adj_open = open * adj_close / close).
# Compute returns from adj_open/adj_close only.
PRICE_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume", "adj_open", "adj_close"]

# Features on trading days. `date` = the trading day t whose cutoff the feature is known by.
FEATURE_COLUMNS = ["date", "ticker", "feature", "value"]

# Forward returns for a signal formed at day t's cutoff, entered at t+1 open.
#   ret_oo:        adj_open[t+1+h] / adj_open[t+1] - 1           (the tradable label)
#   ret_overnight: adj_open[t+1] / adj_close[t] - 1              (NOT tradable from t's signal; diagnostic)
#   ret_intraday:  adj_close[t+1] / adj_open[t+1] - 1            (first day only)
LABEL_COLUMNS = ["date", "ticker", "horizon", "ret_oo", "ret_overnight", "ret_intraday"]

# Forward realised volatility for hypothesis A4: daily-unit RMS of close-to-close log returns
# over t+1..t+horizon (core/labels.realized_vol_label).
VOL_LABEL_COLUMNS = ["date", "ticker", "horizon", "value"]

# Cross-sectional scores (higher = more long), produced by core/models.py rules and models.
SCORE_COLUMNS = ["date", "ticker", "score"]
# Stitched out-of-sample scores; `path` is the CPCV backtest path id (0 for walk-forward).
OOS_COLUMNS = ["date", "ticker", "path", "score"]

# Target portfolio weights decided at day t's cutoff, executed at t+1 open.
WEIGHT_COLUMNS = ["date", "ticker", "weight"]

# Daily strategy returns of one trial (net of costs). `date` is the END of the open-to-open
# interval: the return dated d covers adj_open(d-1) -> adj_open(d) (core/simulate.py).
TRIAL_RETURN_COLUMNS = ["trial_id", "date", "ret"]


# --- Records --------------------------------------------------------------------------------

class Asset(BaseModel):
    ticker: Ticker
    name: str
    kind: Literal["etf", "stock"]
    track: Track
    group: str = Field(description="market | country | sector | commodity | rates | sp500 | sp400")
    # Track A: GDELT ActionGeo country codes are FIPS 10-4 (e.g. JA=Japan, GM=Germany), not ISO.
    fips_countries: list[str] = []
    gkg_themes: list[str] = []
    # Track B: lowercase strings as they appear in GKG V2Organizations.
    org_aliases: list[str] = []
    first_trade: date | None = None


class SignalReport(BaseModel):
    """Univariate evaluation of one feature against one label horizon (cross-sectional IC)."""
    feature: str
    horizon: int
    track: Track
    n_days: int
    ic_mean: float
    ic_std: float
    ic_t: float  # Newey-West adjusted t-stat of the daily IC series
    ic_hit_rate: float
    q_spread_mean: float  # mean top-minus-bottom quantile return per period
    turnover: float  # mean fraction of the top/bottom quantile sets replaced per day
    decay: dict[int, float] = {}  # horizon -> ic_mean


class BacktestStats(BaseModel):
    n_days: int
    ann_return: float
    ann_vol: float
    sharpe: float
    max_drawdown: float
    turnover: float  # mean daily sum|traded notional| / pre-trade portfolio value
    cost_drag: float  # annualised return lost to costs and borrow
    # ann_return is geometric (252-day year); sharpe is annualised; max_drawdown is <= 0.


class Trial(BaseModel):
    """One evaluated configuration. Every trial is logged, including failures — DSR/PBO need all."""
    trial_id: TrialId
    created_at: datetime
    track: Track
    stage: Literal["signal", "model", "backtest", "holdout"]
    hypothesis: str
    params: dict
    data_start: date
    data_end: date
    stats: BacktestStats | None = None
    notes: str = ""


class OverfitReport(BaseModel):
    n_trials: int
    best_trial_id: TrialId
    best_sharpe: float  # ANNUALISED (sqrt(252))
    deflated_sharpe: float  # a probability: P(best Sharpe > 0) after selection over n_trials
    pbo: float | None  # probability of backtest overfitting (CSCV); None if too few trials
    sharpe_ci95: tuple[float, float]  # ANNUALISED stationary-bootstrap CI of the best Sharpe


class BqUsage(BaseModel):
    query_name: str
    ran_at: datetime
    bytes_processed: int
    bytes_billed: int
    dry_run: bool
