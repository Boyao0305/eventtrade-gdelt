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
| `ingest-gdelt {events,gkg-themes,gkg-orgs}` | `--start` (2015-03-02) `--end` (2025-03-31) `--execute` (off) | **Without `--execute` it only dry-runs**: prints the byte estimate per pending month and the budget left. With `--execute` it pulls month by month and upserts each month in one transaction. Themes come from Track A assets, aliases from Track B assets. Refuses `--end` in the hold-out. |
| `build-dataset` | `--track A\|B` (required) | Features (pre-registered families, see `cli/pipelines.py` `_FAMILIES`) and labels at horizons 1, 5, 20 over the dev period; replaces that track's rows. |
| `signal-study` | `--track` (required) | Every stored feature x horizon: IC, Newey-West t, sub-period signs, quantile spread, gate 1 verdict. Gate 1 uses each feature's pre-registered sign (ADR 0002); `sm5` features run at h = 5 and 20 only. Each is logged as a `signal` trial with `features_version` and `expected_sign` in its params (re-running the same study logs nothing new). Track A also tests A1 features against the volatility surprise, with vol controls and a placebo (A4, ADR 0002). Exit 4 and nothing logged if the stored features predate features v2 (no `sm5` feature): rerun `build-dataset` first. |
| `screen` | `--track A\|B` (required), `--n-boot` (2000, at least 999), `--set s3\|a2` (`s3`) | `--set s3` (default): the pre-registered sub-category screen of ADR 0003, described in the rest of this cell. `--set a2`: the ADR 0004 GKG theme screen, Track A only (exit 2 on Track B). It uses the same statistics on 8 cells: signals `A2.attention` (mentions of the ETF's mapped themes / all GKG docs that day) and `A2.tone` (mention-weighted mean tone) x groups commodity (USO UNG GLD SLV CPER DBA XLE) / sector (XLB XLC XLF XLI XLK XLP XLRE XLU XLV XLY ITA) x h 5/20. Trials use hypothesis `A2`, and their params add `hypothesis` and `themes_hash`. Exit 4, before any theme value is read, unless every dev month of `gkg_themes_daily` is stored (what `ingest-gdelt gkg-themes` reports as 0 to pull) and every listed ETF is a stored asset with themes. The S3 description follows. Builds the S3 signals in memory (`sm5_z.60`; never stored) and forms cells = signal x asset group (`assets.screen_group`: A dm/em/commodity, B GICS sector) x horizon 5/20. Every cell counts: one with fewer than 5 names on most discovery days is kept with p = NaN (counted in BH's m, never discovered) and logged noting "insufficient names". Discovery 2015-03-02..2019-12-31 (labels embargoed to end by 2019-12-31): per-cell IC and NW t, joint stationary-bootstrap test (seed = config seed, mean block 40; significant when min(p_S, p_M) < 0.025). If not significant the track stops; otherwise EB shrinkage, Benjamini-Hochberg q = 0.10, then confirmation 2020-01-01..2025-03-31 of the discovered cells only (one-sided, Holm 5%, spread after 4 x cost x turnover). Each cell x period is logged as a `signal` trial, hypothesis `S3`, whose params include n_boot, mean block, seed and a hash of the cell's members; re-running with the same settings logs nothing new, any other setting logs new trials. Exit 2 if `--n-boot` < 999; exit 4 if no asset has a `screen_group` (rerun `load-universe`). |
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
- `build-dataset` replaces the track's features and labels; `signal-study`, `screen` and `backtest` derive a
  deterministic trial id from the configuration and data window, so repeats are not new trials.
- What has been done so far is always `eventtrade status`, never a file.

Agent session notes: [`docs/agent/log.md`](docs/agent/log.md). Traps: [`CLAUDE.md`](CLAUDE.md).
