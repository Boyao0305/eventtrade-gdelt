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
