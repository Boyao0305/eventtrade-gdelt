# eventtrade

Research system for one question: do GDELT news events, measured before a 15:30 New York cutoff,
predict next-day-open returns of country/sector/commodity ETFs (Track A) or S&P 500/400 stocks
(Track B), after costs and after deflating for every configuration tried?

The hypotheses, gates and the hold-out split are pre-registered in
[`docs/adr/0001-preregistration-and-holdout.md`](docs/adr/0001-preregistration-and-holdout.md).
A negative result is the expected outcome; the system is built so that a negative is cheap and
trustworthy.

One container (this CLI), one SQLite file, two external sources: GDELT 2.0 on BigQuery and daily
prices from Yahoo (yfinance). Module map: [`docs/components/cli.md`](docs/components/cli.md).
Vocabulary and domain rules: [`docs/domain.md`](docs/domain.md).

Applied from `convention.md`: src layout, schemas, tests, import contracts, component doc with a
stage view (pipeline with several inputs and outputs), ADR, experiments, domain.md, agent log.
No `containers.md` (one process) and no `schema.sql` (tables live in `adapters/store.py`).

## Setup

Python 3.12 only.

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
export EVENTTRADE_HOME=/path/to/gdelt        # holds data/ and secrets/; never committed
```

`EVENTTRADE_HOME` (or `--home`) is the directory holding `data/eventtrade.sqlite` and
`secrets/<service-account>.json`. When working from a git worktree, point it at the main checkout,
where the data and the key live. The GCP key defaults to the single `*.json` in
`<home>/secrets/`; pass `--gcp-key` if there are several.

Tests: `.venv/Scripts/python -m pytest -q` (fast; `-m slow` runs the network/BigQuery ones).
Layering: `.venv/Scripts/lint-imports` (on a non-UTF-8 Windows console set `PYTHONUTF8=1`).

## CLI

```
eventtrade [--home DIR] [--gcp-key FILE] <command> [options]
python -m eventtrade.cli.main ...            # same thing without the console script
```

| Command | Options (default) | Does |
|---|---|---|
| `status` | | Row count and min/max date per table, BigQuery bytes billed this UTC month vs the 900 GB budget, trial counts per track and stage. |
| `init-db` | | Creates the SQLite store and tables. Idempotent. |
| `load-universe` | `--track A\|B` (both) | A: the packaged ETF list (`universe_etf.csv`). B: current S&P 500 + 400 members from Wikipedia, with GKG organisation aliases from `core/aliases` (ambiguous ones dropped), one share class per company. A reloaded track is replaced: its stored tickers no longer listed are deleted from `assets` (prices and trials stay). |
| `ingest-prices` | `--start` (2015-03-02) `--end` (2025-03-31, inclusive) `--tickers` (all stored assets) `--track` | yfinance daily bars, 100 tickers per upsert. Refuses `--end` in the hold-out. |
| `ingest-gdelt {events,gkg-themes,gkg-orgs}` | `--start` (2015-03-02) `--end` (2025-03-31) `--execute` (off) `--budget-gb` (config, 900) | **Without `--execute` it only dry-runs**: prints the byte estimate per pending month and the budget left. With `--execute` it pulls month by month and upserts each month in one transaction. `--budget-gb` raises the month-to-date BigQuery budget for this run only; above 4,600 GB (the user's $20 cap over the free 1 TiB) it is a usage error (exit 2). Themes come from Track A assets, aliases from Track B assets. Refuses `--end` in the hold-out. |
| `ingest-intraday` | `--start` (2016-01-04) `--end` (2025-03-31) `--tickers` (hedges, then Track A, then Track B) `--max-tickers` | ADR 0007: Alpaca 15-minute SIP bars (regular session, `adjustment=all`; key in `secrets/alpaca.env`) into `data/intraday.sqlite`, one transaction per (ticker, calendar year) recorded in a ledger, so reruns resume. Alpaca has no bars before 2016. Refuses `--end` in the hold-out. ~15 requests per ticker-year at <= 180/min. |
| `intraday-status` | — | Row counts of `data/intraday.sqlite`. |
| `intraday-study` | — | ADR 0007, once only: 18 cells (I1–I6) on discovery 2016-01-04..2019-12-31 (placebo-day p, 2,000 draws, BH q = 0.10), then confirmation 2020-01-02..2025-03-31 of discovered cells (Holm, net of costs, momentum residual). Logs every cell x period as a `signal` trial. If any I1–I6 trial exists it recomputes nothing and prints the stored verdicts. Exit 4 without SPY/hedge bars or 15-minute GDELT rows. |
| `ingest-gdelt-15min {events,orgs,events_cat}` | `--start` (2015-12-01) `--end` (2025-03-31) `--execute` (off) `--budget-gb` (config, 900) | ADR 0007/0008: 15-minute GDELT batch counts into `data/intraday.sqlite` (`events_15min` per FIPS country of the Track A dm/em/commodity assets and `_GLOBAL`; `orgs_15min` per Track B ticker, one count per document however many of its aliases match, and `_GLOBAL`; `events_15min_cat` (ADR 0008) the same eligible event rows per (country, geo, batch) with QuadClass counts q1-q4, root counts r14/r16-r20, base count b163, articles and tone: geo `action` = ActionGeo country and `_GLOBAL` (dedup as `events_15min`), geo `actor` = Actor1/Actor2CountryCode mapped from CAMEO to FIPS by `adapters/cameo_fips.csv`, deduplicated per (ET day, URL, EventCode, actor country), so once per distinct country; exit 4 if a Track A country has no CAMEO code). Days are America/New_York calendar days; `events` and `orgs` keep only batches on weekdays at 12:00-19:45 UTC, `events_cat` keeps every batch (weekends, all 96 UTC slots). **Without `--execute` it only dry-runs** and prints the estimate per pending chunk (calendar years, clipped to the range). With `--execute` each chunk is written with its row in the `gdelt_pulls` ledger in one transaction; chunks already in the ledger are skipped, so a run stopped by the budget guard (exit 3) resumes. `--budget-gb` as for `ingest-gdelt`. Exit 4 without assets; refuses `--end` in the hold-out. |
| `ingest-sec-extra` | — | ADR 0011 amendments 2–3, into `data/sec.sqlite` (needs `ingest-sec`'s CIK map and 13D list). Track B tickers are mapped to CIKs by exact ticker equality with `cik_tickers` (match rate printed). (a) `company_filings`: each mapped company's 10-Q/10-K (+/A), 8-K (with item string) and DEFM14A filings dated 2015-01-01..2025-03-31 from the EDGAR submissions API, all pages. (b) `subject_13d`: the header SUBJECT COMPANY CIK (`-index-headers.html`) of every initial 13D (`SC 13D`/`SCHEDULE 13D`) filed before the hold-out that lists a universe CIK. One ledger row per CIK / accession, so reruns resume. Reads no prices. Exit 4 without Track B assets or the CIK map. |
| `ingest-dod` | `--limit` (all) `--relist` (off) | DoD daily contract announcements ("Contracts For <date>", ~17:00 ET) into `data/dod.sqlite`, read from Wayback Machine captures (defense.gov returns 403 to direct fetches). The CDX listing of three URL prefixes is cached in `dod_snapshots` (`--relist` lists again); each article id is fetched at <= 1 request/s, best capture first (up to 3), and recorded in `dod_articles` with a status; awards (agency, awardees, first `$` amount, kind, small-business flag, paragraph) go to `dod_awards` only for release dates 2015-01-01..2025-03-31 (later articles: `out_of_range`, date read, awards not parsed). Articles already `ok`/`out_of_range`/`not_contracts` are skipped, so reruns resume. Prints progress every 100 articles and a summary by year. Reads no prices. Exit 1 if some articles failed (rerun retries them). |
| `build-dataset` | `--track A\|B` (required) | Features (pre-registered families, see `cli/pipelines.py` `_FAMILIES`) and labels at horizons 1, 5, 20 over the dev period; replaces that track's rows. |
| `signal-study` | `--track` (required) | Every stored feature x horizon: IC, Newey-West t, sub-period signs, quantile spread, gate 1 verdict. Gate 1 uses each feature's pre-registered sign (ADR 0002); `sm5` features run at h = 5 and 20 only. Each is logged as a `signal` trial with `features_version` and `expected_sign` in its params (re-running the same study logs nothing new). Track A also tests A1 features against the volatility surprise, with vol controls and a placebo (A4, ADR 0002). Exit 4 and nothing logged if the stored features predate features v2 (no `sm5` feature): rerun `build-dataset` first. |
| `screen` | `--track A\|B` (required), `--n-boot` (2000, at least 999), `--set s3\|a2` (`s3`) | `--set s3` (default): the pre-registered sub-category screen of ADR 0003, described in the rest of this cell. `--set a2`: the ADR 0004 GKG theme screen, Track A only (exit 2 on Track B). It uses the same statistics on 8 cells: signals `A2.attention` (mentions of the ETF's mapped themes / all GKG docs that day) and `A2.tone` (mention-weighted mean tone) x groups commodity (USO UNG GLD SLV CPER DBA XLE) / sector (XLB XLC XLF XLI XLK XLP XLRE XLU XLV XLY ITA) x h 5/20. Trials use hypothesis `A2`, and their params add `hypothesis` and `themes_hash`. Exit 4, before any theme value is read, unless every dev month of `gkg_themes_daily` is stored (what `ingest-gdelt gkg-themes` reports as 0 to pull) and every listed ETF is a stored asset with themes. The S3 description follows. Builds the S3 signals in memory (`sm5_z.60`; never stored) and forms cells = signal x asset group (`assets.screen_group`: A dm/em/commodity, B GICS sector) x horizon 5/20. Every cell counts: one with fewer than 5 names on most discovery days is kept with p = NaN (counted in BH's m, never discovered) and logged noting "insufficient names". Discovery 2015-03-02..2019-12-31 (labels embargoed to end by 2019-12-31): per-cell IC and NW t, joint stationary-bootstrap test (seed = config seed, mean block 40; significant when min(p_S, p_M) < 0.025). If not significant the track stops; otherwise EB shrinkage, Benjamini-Hochberg q = 0.10, then confirmation 2020-01-01..2025-03-31 of the discovered cells only (one-sided, Holm 5%, spread after 4 x cost x turnover). Each cell x period is logged as a `signal` trial, hypothesis `S3`, whose params include n_boot, mean block, seed and a hash of the cell's members; re-running with the same settings logs nothing new, any other setting logs new trials. Exit 2 if `--n-boot` < 999; exit 4 if no asset has a `screen_group` (rerun `load-universe`). |
| `event-study` | `--track A\|B` (required), `--n-boot` (2000, at least 999) | The pre-registered extreme-shock event study of ADR 0005. It builds shock scores (v2 z vs a trailing W = 60/120-day baseline, no smoothing) and outcomes (abnormal return vs the other group members, raw return for E2, abnormal volatility; h = 1/5/10/20, entered at t+1 open) in memory from dev data. Nothing is stored except trials. Track A has 44 cells (E1, E1c, E2, E3) and Track B has 3 (E4, E4p, E5), each with 32 configurations (theta 2/2.5/3/4 x W x h). Events are declustered (20 trading days per asset and trigger signal). E4/E4p split the declustered attention events by same-day tone z. Discovery covers 2015-03-02..2019-12-31, with events embargoed so their outcome ends by 2019-12-31. Each cell x configuration gets a calendar-time series (each day, the mean outcome of the events held that day) and NW t, and is logged as a `signal` trial (hypothesis = E-id). A configuration with < 30 events or < 30 distinct event dates is logged as "insufficient events" and cannot be selected. Per cell, the largest directional t is selected, and p is the placebo-date max-t p over the sufficient configurations (ADR 0005 Amendment: each of n-boot draws moves every event date, with its tickers, to a random date of the discovery window, one mapping shared by all configurations; seed = config seed). **BH q = 0.10 runs across all 47 cells, so it needs the other track's logged discovery with the same settings.** Until then the run stops after discovery: run both tracks, then rerun the first. Discovered cells are confirmed once on 2020-01-01..2025-03-31 with their selected configuration. Confirmation needs a one-sided placebo-date p < 0.05 after Holm across the discovered cells of both tracks, and a mean net of costs > 0 in the registered direction (4 legs for abnormal return, 2 for raw, 5 bp ETF / 10 bp stock per leg; vol cells have no cost). Each confirmation is logged as a trial. Holm is reported as pending until the other track's discovered cells have logged confirmations; the run that completes Holm records every discovered cell's final verdict in its notes (`holm_verdicts`). Params: `cell, theta, window, horizon, period, features_version, expected_sign, n_boot, null, seed, members_hash` (`null` = `placebo_dates`). A rerun with the same settings logs nothing new. Exit 2 if `--n-boot` < 999; exit 4 without grouped assets, prices or GDELT rows. |
| `followup` | `--id F1` (required; the only id). No `--n-boot`: ADR 0006 fixes 2,000 draws | The single pre-registered follow-up test of ADR 0006: do conflict news shocks raise the abnormal volatility of EM country ETFs? Assets: Track A ETFs with `screen_group` `em`. Composite event: an ETF fires on day t if quad3, quad4, protest (root 14) or violence share z >= 3, or tone z <= -3 (W = 60). One 20-trading-day declustering per ETF runs over that union. Outcome: ADR 0005 abnormal volatility at h = 20. Window: events dated 2020-01-01..2025-03-31, embargoed so their outcome ends by 2025-03-31 (the hold-out is never read). Statistic: calendar-time mean and NW t. Test: one-sided (+) placebo-date p (2,000 draws, seed = config seed). With fewer than 30 events or 30 event dates the verdict is `inconclusive` and no p is computed. Otherwise `pass` iff p < 0.05 and mean > 0, else `fail`. Logs exactly one `signal` trial, hypothesis `F1`, params `hypothesis, group, signals, theta, window, horizon, decluster_days, outcome, expected_sign, period, alpha, min_events, min_event_days, n_boot, null, seed, features_version, members_hash`. Once any trial with hypothesis `F1` is logged, every later run (whatever the seed, features version or EM members) reads and computes nothing, logs nothing and prints the stored verdict. Exit 2 if `--n-boot` is given or `--id` is not F1; exit 4 without EM assets, prices or GDELT rows. |
| `backtest` | `--track` (required), one of `--feature NAME` / `--model ridge\|logistic\|lgbm`; `--sign` (pre-registered sign; required for H0 features, which have none: exit 2 without it), `--features` (default: every stored feature, chosen per walk-forward fold by the gate-1 IC test on that fold's training dates only), `--horizon` (5), `--rebalance` (= horizon) | Rank long-short weights, executed at the next open by `core/simulate`, net of costs and borrow. Models are walk-forward (504-day expanding train, 63-day test, purge = horizon). Logs a `backtest` trial with its daily returns. |
| `overfit-report` | `--track` (required) | Deflated Sharpe of the best backtest trial, with N = every logged signal/model/backtest trial of the track, PBO (CSCV), the best trial's bootstrap 95% CI, gate 3 verdict. |
| `holdout` | `--track`, `--trial-id` (required), `--execute`, `--i-understand-this-is-final` | Refuses unless the last flag is given, and refuses a trial already evaluated. Without `--execute` it only reports and dry-runs the hold-out GDELT queries (fetches and writes nothing). With `--execute` it fetches hold-out prices and GDELT, re-runs a **rule** backtest trial on 2025-04-01..yesterday, logs a `holdout` trial, prints gate 4 (pass only if the bootstrap 95% CI of the Sharpe is entirely above 0). |

Exit codes: `0` ok · `1` partial failure (some tickers failed; the rest are stored and the failed
ones are listed) · `2` refused or usage error (hold-out guard, bad flags, missing
`EVENTTRADE_HOME`) · `3` the BigQuery budget guard refused a query · `4` a prerequisite is missing
(store not initialised, no assets, prices, features or trials, no screen groups, or for
`screen --set a2` an incomplete GKG theme pull).

Changing a flag name, default or exit code is a breaking change: update this table in the same
change.

## Typical run

```bash
eventtrade init-db
eventtrade load-universe
eventtrade ingest-prices
eventtrade ingest-gdelt events                 # read the estimate first
eventtrade ingest-gdelt events --execute
eventtrade ingest-gdelt gkg-themes --start 2021-01-01   # GKG is ~1 TB for 10 years: chunk by year
eventtrade build-dataset --track A
eventtrade signal-study --track A
eventtrade backtest --track A --feature 'events.root_19_fight/n_events.sum_share_z.60'
eventtrade overfit-report --track A
```

## Resuming

Every step is idempotent, so resuming means re-running the same command.

- `ingest-gdelt ... --execute` skips any month whose first and last session dates are already
  stored for that query. A month is written in one transaction, so a crash or a budget refusal
  (exit 3) leaves only whole months. Next month, or after raising the budget, run the same command.
  Caveat: a month whose first or last day is a GDELT outage is never "complete" and is re-pulled.
  The theme and alias lists are not part of the skip check; fix them before pulling (GKG cost does
  not depend on how many are requested, and adding one later means a full re-pull).
- `ingest-prices` overwrites by (date, ticker); re-run it, or pass the failed tickers it printed
  with `--tickers`.
- `build-dataset` replaces the track's features and labels; `signal-study`, `screen`, `event-study`, `followup` and `backtest` derive a
  deterministic trial id from the configuration and data window, so repeats are not new trials.
- What has been done so far is always `eventtrade status`, never a file.

Agent session notes: [`docs/agent/log.md`](docs/agent/log.md). Traps: [`CLAUDE.md`](CLAUDE.md).
