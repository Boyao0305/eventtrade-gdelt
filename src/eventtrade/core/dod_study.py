"""ADR 0015 (DoD contract awards, hypothesis family D): award events, market caps, cells,
declustering, placebo expectations. Pure: no I/O.

Inputs are plain frames: the frozen issuer map (one row per sole-award paragraph), the stored
award paragraphs, SEC share counts, split-adjusted closes (Yahoo, today's share basis) and the
full split history (to undo that adjustment).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

import numpy as np
import pandas as pd

from eventtrade.core.dod_parse import paragraph_amount

CELL_BOUNDS = {"material": (0.01, math.inf), "moderate": (0.001, 0.01)}
CELLS = tuple(CELL_BOUNDS)
HORIZONS = (1, 5, 20)
SHARES_MAX_AGE_DAYS = 400
FLOAT_TAG = "EntityPublicFloat"
DEI_SHARES = "EntityCommonStockSharesOutstanding"
FLOAT_LIMIT = 100.0
SIGMA = {1: 0.02, 5: 0.045, 20: 0.08}   # amendment 11, fixed before outcomes
MIN_EVENTS, MIN_ISSUERS = 40, 15
_DEOBLIGATION = re.compile(r"de-?obligat", re.IGNORECASE)
# amount audit (2026-10-12, 200 paragraphs): 0% parser errors, but 40% ceilings / multi-year
# totals. Ceiling-flagged paragraphs are excluded from size (amendment 1: never a ceiling).
CEILING_WORDS = re.compile(
    r"\b(?:maximum|ceiling(?:[- ]priced)?|not[- ]to[- ]exceed|estimated(?: maximum)? value"
    r"|requirements contract|blanket purchase agreement|indefinite[- ]delivery"
    r"|indefinite[- ]quantity)\b"
    r"|\b(?:work locations? and funding|funding and work locations?) will be determined with"
    r" each order\b"
    r"|funds? will be obligated (?:on|against|as) (?:individual )?(?:task |delivery )?orders",
    re.IGNORECASE)
CEILING_DTYPE = re.compile(r"\b[A-Z0-9]{6}-?\d{2}-?D-?\d{4}\b")  # IDIQ contract number (D type)
MULTI_AWARD = re.compile(
    r"\bone of (?:\w+|\d+) (?:multiple[- ]award )?(?:contracts|awards|BPAs)\b"
    r"|\bcompete for (?:each |individual |future )?(?:task |delivery )?orders\b"
    r"|\bcontractor team arrangement\b", re.IGNORECASE)
_NUMBERED = re.compile(r"\(\d\) An? \$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?", re.IGNORECASE)


def action_amount(text: str) -> float | None:
    """The paragraph's action amount: the sum of numbered actions '(1) A $X ... (2) A $Y' when
    present, else the parser's first dollar amount."""
    nums = _NUMBERED.findall(text or "")
    if len(nums) >= 2:
        return float(sum(float(a.replace(",", "") + (b or "")) for a, b in nums))
    return paragraph_amount(text)


def is_ceiling(text: str) -> bool:
    """Ceiling / indefinite-delivery language, or a D-type (IDIQ) contract number."""
    t = text or ""
    return bool(CEILING_WORDS.search(t) or CEILING_DTYPE.search(t))


def award_rows(mapping: pd.DataFrame, awards: pd.DataFrame,
               exclusions: pd.DataFrame | None = None, with_ceilings: bool = False
               ) -> pd.DataFrame:
    """Mapped sole-award paragraphs with a positive action amount (amendment 1 exclusions:
    corrections, de-obligations, no or non-positive amount; after the amount audit also
    multiple-award language and, unless `with_ceilings`, ceilings). Amounts are re-derived from
    the paragraph text (`action_amount`). `exclusions` (ticker, awardee_norm): wrong-identity
    pairs from the ticker check. Columns: d, cik, ticker, amount, kind, agency, small_business,
    article_id, seq, rule, ceiling."""
    m = mapping[mapping["cik"].notna()].copy()
    m["article_id"] = m["article_id"].astype(int)
    m["seq"] = m["seq"].astype(int)
    a = awards.copy()
    a["article_id"] = a["article_id"].astype(int)
    a["seq"] = a["seq"].astype(int)
    j = m[["article_id", "seq", "cik", "ticker_now", "rule", "norm"]].merge(
        a[["article_id", "seq", "release_date", "kind", "agency", "small_business", "text"]],
        on=["article_id", "seq"], how="inner")
    j["amount"] = j["text"].map(action_amount)
    txt = j["text"].fillna("")
    j["ceiling"] = txt.map(is_ceiling)
    keep = (j["kind"] != "correction") & j["amount"].notna() & (j["amount"] > 0) \
        & ~txt.str.contains(_DEOBLIGATION) & ~txt.str.contains(MULTI_AWARD)
    if not with_ceilings:
        keep &= ~j["ceiling"]
    if exclusions is not None and len(exclusions):
        start = exclusions["from_date"] if "from_date" in exclusions else pd.Series(
            [""] * len(exclusions), index=exclusions.index)
        bad = {(t, n): (str(f) if isinstance(f, str) and f else "")
               for t, n, f in zip(exclusions["ticker"], exclusions["awardee_norm"], start,
                                  strict=True)}
        drop = [(t, n) in bad and str(rd)[:10] >= bad[(t, n)]
                for t, n, rd in zip(j["ticker_now"], j["norm"], j["release_date"], strict=True)]
        keep &= ~pd.Series(drop, index=j.index)
    j = j[keep].copy()
    j["d"] = pd.to_datetime(j["release_date"])
    j["cik"] = j["cik"].astype(str).str.zfill(10)
    j = j.rename(columns={"ticker_now": "ticker"})
    return j[["d", "cik", "ticker", "amount", "kind", "agency", "small_business", "article_id",
              "seq", "rule", "ceiling"]].reset_index(drop=True)


def issuer_days(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per (release day d, issuer CIK): size = sum of action amounts, n paragraphs,
    kinds and agencies joined, small_business = any."""
    g = rows.groupby(["d", "cik"], sort=True)
    out = g.agg(size=("amount", "sum"), n=("amount", "size"), ticker=("ticker", "first"),
                kinds=("kind", lambda s: ",".join(sorted(set(map(str, s))))),
                agencies=("agency", lambda s: ",".join(sorted(set(map(str, s))))),
                small_business=("small_business", "max")).reset_index()
    return out


def filing_shares(shares: pd.DataFrame) -> pd.DataFrame:
    """Per filing (cik, accn): filed date, cover/period end, share count. dei cover counts are
    summed over share classes (distinct values at the filing's latest end); us-gaap weighted
    averages take the mean of the values at that end. Float rows are ignored."""
    s = shares[shares["tag"] != FLOAT_TAG].copy()
    if s.empty:
        return pd.DataFrame(columns=["cik", "accn", "filed", "end", "shares"])
    s["filed"] = pd.to_datetime(s["filed"])
    s["end"] = pd.to_datetime(s["end"])
    s["cik"] = s["cik"].astype(str).str.zfill(10)
    last = s.groupby(["cik", "accn"])["end"].transform("max")
    s = s[s["end"] == last]
    rows = []
    for (cik, accn), g in s.groupby(["cik", "accn"]):
        vals = g["val"].astype(float)
        n = float(vals.drop_duplicates().sum()) if (g["tag"] == DEI_SHARES).all() \
            else float(vals.mean())
        rows.append((cik, accn, g["filed"].min(), g["end"].max(), n))
    return pd.DataFrame(rows, columns=["cik", "accn", "filed", "end", "shares"])


def shares_asof(days: pd.DataFrame, fs: pd.DataFrame) -> pd.DataFrame:
    """days (d, cik) + the latest filing with filed < d and filed >= d - 400 days:
    columns shares, end (NaN/NaT if none)."""
    left = days[["d", "cik"]].copy()
    left["_i"] = np.arange(len(left))
    left = left.sort_values("d")
    right = fs.sort_values("filed")[["cik", "filed", "end", "shares"]]
    got = pd.merge_asof(left, right, left_on="d", right_on="filed", by="cik",
                        allow_exact_matches=False,
                        tolerance=pd.Timedelta(days=SHARES_MAX_AGE_DAYS), direction="backward")
    return got.sort_values("_i")[["shares", "end"]].reset_index(drop=True)


def split_factor_after(splits: pd.DataFrame | None, when: pd.Timestamp) -> float:
    """Product of split ratios dated strictly after `when` (1.0 without splits)."""
    if splits is None or not len(splits) or pd.isna(when):
        return 1.0
    d = pd.to_datetime(splits["date"])
    r = splits["ratio"].astype(float)
    return float(np.prod(r[(d > when) & (r > 0)])) if ((d > when) & (r > 0)).any() else 1.0


def prev_close(close: pd.DataFrame, ticker: str, d: pd.Timestamp) -> float:
    """Split-adjusted close of the last trading day strictly before d (NaN if none)."""
    if ticker not in close.columns:
        return float("nan")
    s = close[ticker]
    s = s[(s.index < d) & s.notna()]
    return float(s.iloc[-1]) if len(s) else float("nan")


def market_caps(days: pd.DataFrame, fs: pd.DataFrame, close: pd.DataFrame,
                splits: Mapping[str, pd.DataFrame]) -> pd.Series:
    """mcap at d = adjusted close(prev day) x shares(cover end) x F(end), F = product of splits
    after the cover end (amendment 6; equivalent to raw close x split-adjusted shares)."""
    sa = shares_asof(days, fs)
    out = np.full(len(days), np.nan)
    for i, (d, tk, sh, end) in enumerate(zip(days["d"], days["ticker"], sa["shares"], sa["end"],
                                             strict=True)):
        if pd.isna(sh) or sh <= 0 or not isinstance(tk, str):
            continue
        c = prev_close(close, tk, d)
        if c > 0:
            out[i] = c * sh * split_factor_after(splits.get(tk), end)
    return pd.Series(out, index=days.index)


def float_outliers(shares: pd.DataFrame, fs: pd.DataFrame, close: pd.DataFrame,
                   splits: Mapping[str, pd.DataFrame], cik_ticker: Mapping[str, str]
                   ) -> dict[str, float]:
    """CIKs whose market cap at a public-float date is >= 100x or <= 1/100 of the reported float
    (amendment 6), with the worst ratio."""
    fl = shares[shares["tag"] == FLOAT_TAG].copy()
    out: dict[str, float] = {}
    if fl.empty:
        return out
    fl["cik"] = fl["cik"].astype(str).str.zfill(10)
    fl["d"] = pd.to_datetime(fl["end"]) + pd.Timedelta(days=1)
    fl = fl[fl["cik"].isin(list(cik_ticker))].drop_duplicates(["cik", "end"])
    if fl.empty:
        return out
    days = pd.DataFrame({"d": fl["d"].to_numpy(), "cik": fl["cik"].to_numpy(),
                         "ticker": [cik_ticker[c] for c in fl["cik"]]})
    mc = market_caps(days, fs, close, splits).to_numpy()
    ratio = mc / fl["val"].astype(float).to_numpy()
    for cik, r in zip(days["cik"], ratio, strict=True):
        if np.isfinite(r) and r > 0 and (r >= FLOAT_LIMIT or r <= 1 / FLOAT_LIMIT):
            worst = max(r, 1 / r)
            out[cik] = max(out.get(cik, 0.0), worst)
    return out


def foreign_otc(ticker: str) -> bool:
    """Amendment 6: OTC ADRs and foreign ordinary shares (5-letter symbols ending in Y or F,
    e.g. BAESY, CMGMF) drop out."""
    t = str(ticker).upper()
    return len(t) == 5 and t.isalpha() and t[-1] in "YF"


def cell_of(ratio: np.ndarray) -> np.ndarray:
    """'material' / 'moderate' / '' per ratio."""
    r = np.asarray(ratio, dtype=float)
    out = np.full(len(r), "", dtype=object)
    for name, (lo, hi) in CELL_BOUNDS.items():
        out[(r >= lo) & (r < hi)] = name
    return out


def decluster(pos: np.ndarray, ticker: np.ndarray, h: int) -> np.ndarray:
    """Keep mask: per ticker (in time order), an event is kept if no kept event of the same
    ticker lies within the previous h trading days (positions)."""
    keep = np.zeros(len(pos), dtype=bool)
    order = np.lexsort((pos, ticker))
    last: dict[object, int] = {}
    for i in order:
        k = ticker[i]
        if k not in last or pos[i] - last[k] > h:
            keep[i] = True
            last[k] = int(pos[i])
    return keep


def placebo_expectation(events: pd.DataFrame, pool: pd.DataFrame) -> float:
    """Expected event-average outcome under the matched null: the mean over events of their own
    (ticker, stratum) pool mean. events: ticker, stratum; pool: ticker, stratum, value."""
    pm = pool.groupby(["ticker", "stratum"])["value"].mean()
    keys = list(zip(events["ticker"], events["stratum"], strict=True))
    vals = [pm.get(k, np.nan) for k in keys]
    v = np.asarray(vals, dtype=float)
    return float(np.nanmean(v)) if np.isfinite(v).any() else float("nan")


def mde(h: int, n: int) -> float:
    """Minimum detectable mean (one-sided 5%, 80% power) with the fixed sigma."""
    return (1.645 + 0.842) * SIGMA[h] / math.sqrt(n) if n > 0 else float("nan")


def underpowered(n_events: int, n_issuers: int) -> bool:
    return n_events < MIN_EVENTS or n_issuers < MIN_ISSUERS
