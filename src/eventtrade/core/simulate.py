"""Turn target weights into net daily strategy returns, via vectorbt.

Timing (the whole point of this module — tests/core/test_simulate.py pins it):

* A weight row dated t (a decision at day t's cutoff) is executed at ``adj_open`` of the first
  price date strictly after t (t+1). It can never earn the close(t) -> open(t+1) gap.
* The portfolio is marked at ``adj_open`` every day, so returns accrue open-to-open, the same
  convention as ``LABEL_COLUMNS.ret_oo``. The return dated d covers open(d-1) -> open(d); fees of
  trades executed at open(d) are charged in the return dated d. With zero costs,
  ``ret[d] = sum_i w_i(t) * (adj_open_i[d] / adj_open_i[d-1] - 1)`` where t is the decision day
  executed at d-1, i.e. ``ret[t+2] = sum_i w_i(t) * ret_oo_i(t, h=1)``.
* The output starts at the first execution day (its return is just the entry fees) and ends at
  the last price date; positions are held after the last weight row.

Weights semantics:

* Each date present in ``weights`` is a full target portfolio; tickers absent on that date are 0.
  Dates absent from ``weights`` mean "no rebalance": positions are held as shares and drift.
* If several decision dates map to the same execution day (e.g. a weekend), the latest wins.
* Missing ``adj_open`` for a ticker on its execution day: that ticker's order is skipped and its
  current position (shares) is held until the next weight row; it is valued at its last known
  ``adj_open`` meanwhile. Other tickers on that day still trade.

Costs:

* Fees = ``cost_bps / 1e4`` of traded notional, charged by vectorbt in cash (so already in the
  portfolio value). ``cost_bps`` may be a per-ticker dict.
* Borrow: vectorbt has none. Each day, ``borrow_bps_annual / 1e4 / 252`` times the short notional
  held over the interval (shares after the previous day's trades, valued at the previous open) as
  a fraction of the previous day's portfolio value is subtracted from that day's return. It is an
  additive deduction on the return series and does not feed back into vectorbt's cash.

Shorts are allowed (direction both), cash is shared across tickers and ``call_seq='auto'`` sells
before it buys, so a rebalance from one fully-invested name into another does not starve for cash.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import vectorbt as vbt

from eventtrade.schemas import BacktestStats

TRADING_DAYS = 252


def simulate(
    weights: pd.DataFrame,
    prices: pd.DataFrame,
    cost_bps: float | dict[str, float],
    borrow_bps_annual: float,
    init_cash: float = 1e6,
) -> tuple[pd.DataFrame, BacktestStats]:
    """Execute WEIGHT_COLUMNS targets at next-day adj_open; return net daily [date, ret] and stats."""
    target, open_px, missing = _build_matrices(weights, prices)
    fees = _fee_vector(cost_bps, list(target.columns))

    size = target.mask(missing)  # skipped order where the execution-day price is missing
    pf = vbt.Portfolio.from_orders(
        close=open_px,  # mark-to-market at the open -> open-to-open returns
        size=size,
        size_type="targetpercent",
        direction="both",
        price=open_px,
        fees=fees,
        init_cash=float(init_cash),
        group_by=True,
        cash_sharing=True,
        call_seq="auto",
        freq="1D",
    )

    value = pf.value().to_numpy(dtype=float)
    cash = pf.cash().to_numpy(dtype=float)
    assets = pf.assets().to_numpy(dtype=float)
    px = open_px.to_numpy(dtype=float)
    n_rows = len(open_px)

    prev_value = np.concatenate([[float(init_cash)], value[:-1]])
    prev_cash = np.concatenate([[float(init_cash)], cash[:-1]])
    prev_assets = np.vstack([np.zeros((1, assets.shape[1])), assets[:-1]])
    pre_trade_value = prev_cash + (prev_assets * px).sum(axis=1)

    rec = pf.orders.records
    notional = np.bincount(rec["idx"], weights=np.abs(rec["size"] * rec["price"]), minlength=n_rows)
    fees_paid = np.bincount(rec["idx"], weights=rec["fees"], minlength=n_rows)

    prev_px = np.vstack([px[:1], px[:-1]])
    short_notional = (np.clip(-prev_assets, 0.0, None) * prev_px).sum(axis=1)
    borrow_frac = borrow_bps_annual / 1e4 / TRADING_DAYS * short_notional / prev_value

    ret = value / prev_value - 1.0 - borrow_frac
    turnover = float(np.mean(notional / pre_trade_value))
    cost_drag = float(np.mean(fees_paid / prev_value + borrow_frac) * TRADING_DAYS)

    dates = open_px.index
    if not pd.api.types.is_datetime64_any_dtype(prices["date"]):
        dates = dates.date
    out = pd.DataFrame({"date": dates, "ret": ret})
    return out, stats_from_returns(out["ret"], turnover, cost_drag)


def stats_from_returns(ret: pd.Series | pd.DataFrame, turnover: float, cost_drag: float) -> BacktestStats:
    """Summarise a daily net return series (252-day year, zero risk-free rate) into BacktestStats."""
    r = (ret["ret"] if isinstance(ret, pd.DataFrame) else ret).dropna().to_numpy(dtype=float)
    n = len(r)
    if n == 0:
        raise ValueError("stats_from_returns: empty return series")
    equity = np.cumprod(1.0 + r)
    ann_return = float(equity[-1] ** (TRADING_DAYS / n) - 1.0) if equity[-1] > 0 else -1.0
    std = float(np.std(r, ddof=1)) if n > 1 else 0.0
    ann_vol = std * np.sqrt(TRADING_DAYS)
    sharpe = float(np.mean(r) / std * np.sqrt(TRADING_DAYS)) if std > 0 else 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))
    max_dd = float(np.min(np.concatenate([[1.0], equity]) / peak - 1.0))
    return BacktestStats(
        n_days=n,
        ann_return=ann_return,
        ann_vol=float(ann_vol),
        sharpe=sharpe,
        max_drawdown=max_dd,
        turnover=float(turnover),
        cost_drag=float(cost_drag),
    )


def _build_matrices(
    weights: pd.DataFrame, prices: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Wide (execution-date x ticker) targets, filled adj_open, and missing-price mask."""
    if weights.empty:
        raise ValueError("simulate: weights is empty")
    if weights["weight"].isna().any():
        raise ValueError("simulate: NaN weights; use 0 for flat")
    w = weights.assign(date=pd.to_datetime(weights["date"]))
    if w.duplicated(["date", "ticker"]).any():
        raise ValueError("simulate: duplicate (date, ticker) in weights")
    tickers = sorted(w["ticker"].unique())

    p = prices.assign(date=pd.to_datetime(prices["date"]))
    p = p[p["ticker"].isin(tickers)]
    unknown = set(tickers) - set(p["ticker"].unique())
    if unknown:
        raise ValueError(f"simulate: no prices for {sorted(unknown)}")
    open_raw = p.pivot(index="date", columns="ticker", values="adj_open").reindex(columns=tickers).sort_index()
    calendar = open_raw.index

    wide = w.pivot(index="date", columns="ticker", values="weight").reindex(columns=tickers).fillna(0.0)
    pos = calendar.searchsorted(wide.index, side="right")  # first price date strictly after t
    keep = pos < len(calendar)
    if not keep.any():
        raise ValueError("simulate: no weight date has a later price date to execute on")
    wide = wide[keep]
    wide.index = calendar[pos[keep]]
    wide = wide[~wide.index.duplicated(keep="last")]

    start = wide.index[0]
    open_raw = open_raw.loc[start:]
    target = wide.reindex(open_raw.index)  # NaN row = no rebalance that day
    missing = open_raw.isna()
    open_px = open_raw.ffill().fillna(1.0)  # leading gaps: no position can exist there yet
    open_px.columns.name = None
    target.columns.name = None
    return target, open_px, missing


def _fee_vector(cost_bps: float | dict[str, float], tickers: list[str]) -> np.ndarray:
    """Per-ticker fee fraction aligned to columns."""
    if isinstance(cost_bps, dict):
        absent = [t for t in tickers if t not in cost_bps]
        if absent:
            raise ValueError(f"simulate: cost_bps missing tickers {absent}")
        return np.array([[cost_bps[t] / 1e4 for t in tickers]], dtype=float)
    return np.full((1, len(tickers)), cost_bps / 1e4, dtype=float)
