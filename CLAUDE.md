# CLAUDE.md — eventtrade

Read `convention.md` (how the repo is organised) and `docs/adr/0001-preregistration-and-holdout.md`
(what we are allowed to look at) before changing anything.

## Operations that must not run without asking

- **BigQuery queries over 50 GB, or any query without a dry run first.** Free tier is 1 TB/month.
  `adapters/gdelt_bq.py` dry-runs, checks month-to-date usage in the `bq_usage` table, and sets
  `maximum_bytes_billed`. Never bypass it with a raw client call.
- **Touching hold-out data** (dates ≥ `config.HOLDOUT_START`, 2025-04-01) in any analysis other
  than the final, once-per-strategy hold-out evaluation.
- **Committing anything under `secrets/` or `data/`.**
- **Spending more than $20 in total.** Billing is enabled (card connected, 2026-10-06). BigQuery
  on-demand is $6.25/TiB after the free 1 TiB per month. The user's cap: never more than $20
  without explicit approval. `config.bq_monthly_budget_bytes` (default 900 GB) is the guard;
  raise it only as far as the cap allows, and say so in the log.
- **Pushing to any remote other than `origin` (private `Boyao0305/eventtrade-gdelt`), or pushing
  `main`.** Push `dev` only.
- **Re-running a once-only test.** These refuse by design once their hypothesis has trials:
  `followup --id F1`, `intraday-study` (I1–I6), `intraday-search` (J), `foresight` (O/R),
  `foresight-country` (O2), `sec-study` (S1). All of them log only after everything is computed,
  so a crashed run logs nothing and can simply be rerun.
  Never delete or alter trials to make room — the table is append-only and feeds the DSR count.

## Traps

- **`DATEADDED`, never `SQLDATE`, for timing.** SQLDATE is when the event happened and can be
  months or years before publication. DATEADDED is UTC, 15-minute resolution.
- **GDELT event geography uses FIPS 10-4 country codes** (`JA` Japan, `GM` Germany, `UK` UK,
  `CH` China, `SF` South Africa) — not ISO. Actor codes are CAMEO three-letter codes, different again.
- **GDELT volume grows over time** (more sources each year). Raw counts trend; normalise by the
  global daily total before z-scoring.
- **vectorbt fills at the close by default.** Signals decided at day t must be executed at t+1
  `adj_open`. `core/simulate.py` owns this; tests in `tests/core/test_simulate.py` pin it.
- **Running from a worktree:** data and secrets live in the main checkout. Set
  `EVENTTRADE_HOME=D:/programmation/Github/b2b_approach/team_tool/workspace/gdelt`.
- **Python 3.12 only** (`.venv`, created with `uv`). Run tests with `.venv/Scripts/python -m pytest`.
- **`events_partitioned` is partitioned by SQLDATE, not DATEADDED** (experiment 0001). A DATEADDED
  window must scan a lookback of earlier partitions; that is why events cost more per month than
  their row count suggests.
- **Do not install or upgrade packages in `.venv` while a pull is running.** On 2026-10-05 an
  install killed both running `ingest-gdelt` processes silently (no traceback). Months already
  written survive; rerun the same command and it resumes.
- **The service account cannot use the BigQuery Storage API** (403 `bigquery.readsessions.create`).
  Keep `google-cloud-bigquery-storage` uninstalled, otherwise result downloads fail instead of
  falling back to REST. Downloads are the bottleneck (~2 min per month per source): speed up by
  running `ingest-gdelt` concurrently over disjoint `--start/--end` ranges (same BigQuery cost).
- **First import of the heavy stack is slow** (~4 min after a fresh install, vectorbt ~16–40 s
  after). `lint-imports` needs `PYTHONUTF8=1` on this console.
- **After a change to the store's tables, run `init-db` first.** Migrations (e.g. the
  `assets.screen_group` column) live in `SqliteStore.init()`; `load-universe` and the other
  commands do not call it and fail with "no column named ...".
- **Never run a trial-logging command (`signal-study`, `screen`, `backtest`) while code on `dev`
  is being edited.** Trials are append-only; run from a frozen checkout (`git worktree add
  --detach`) with `PYTHONPATH=<checkout>/src` if edits are in flight.
- **GKG themes for the full dev range cost ~1 TB** — more than one month's free tier. Pull them in
  a later billing month (see experiment 0001's plan), or within the $20 cap with billing.
- **Run the full test suite in foreground chunks (< 10 min each)**: `tests/core tests/adapters`,
  `tests/cli` minus `test_event_study.py`/`test_followup.py`, then those two (split
  `test_event_study.py` in halves via `--co -q`). A long background pytest gets reaped by
  Claude Code when the machine is low on memory (2026-10-06).
- **This harness rejects complex shell lines whose paths contain "Github"** (worktree guard).
  Put multi-step commands in a script under the job's tmp dir and run `bash <script>`; wrappers
  `tmp/et.sh` (dev code) and `tmp/et_snap.sh` (frozen checkout `.claude/worktrees/gdelt-snap`)
  set `EVENTTRADE_HOME`, `PYTHONUTF8` and, for the snapshot, `PYTHONPATH`.
- **Python stdout is block-buffered when redirected to a file**: progress lines of long CLI runs
  appear only at the end. Use `status` (row counts, BigQuery spend) to watch progress.
- **Concurrent heavy SQLite writes lock out other writers** (60 s timeout): run `build-dataset`
  for A and B one after the other, never alongside a trial-logging command.
- **Stores:** `data/eventtrade.sqlite` (main, trials), `data/intraday.sqlite` (Alpaca 15-minute
  bars, 15-minute GDELT), `data/sec.sqlite` (SEC filings).
- **Alpaca** (free paper key in `secrets/alpaca.env`): historical SIP bars start on 2016-01-01,
  with pages of ~900 bars and ≤ 200 requests/min. The IEX feed is useless before 2020.
- **SEC EDGAR:** the User-Agent must carry the user's email (`config.sec_user_agent`, approved
  2026-10-10), with ≤ 10 requests/s. The insider data sets give FILING_DATE without a time.
  Filings after 22:00 (Form 4) or 17:30 (13D) are dated the next day, so the entry is the
  t+1 open.
- **`company_tickers.json` is today's ticker → today's CIK.** Holding-company reorganizations
  (DIS, CI, AVGO, LIN, XOM 2025…) lose earlier filings. Never match by ticker symbol: symbols
  are reused (pre-2020 IR = Trane).
- **Look-ahead diagnostics** (ADR 0009/0010) log trials with `params.lookahead = true`.
  `pipelines.counts_for_dsr` excludes them from DSR's N, and they must never be backtested.
- **Long background jobs get reaped** when the machine is low on memory and the session is idle
  (an Alpaca pull was killed). For multi-hour jobs, poll actively, or ask the user to run them in
  their own terminal. In PowerShell use `--home <path>`: `set VAR=` does nothing there.
- **Windows Smart App Control** blocked `.venv\Scripts\python.exe` and scikit-learn's `.pyd`
  from 2026-10-09; the user turned it off on 2026-10-10.
  - If it comes back, the workaround is base CPython 3.12
    (`%APPDATA%\uv\python\cpython-3.12-windows-x86_64-none\python.exe`) with
    `PYTHONPATH=src;.venv/Lib/site-packages`.
  - scikit-learn, vectorbt and skfolio stay blocked under it.
- **Before a trial-logging run, a timing-only profile is fine** (stores wrapped so `add_trial` is
  a no-op, statistics never printed). Printing any outcome statistic before the run is not.
