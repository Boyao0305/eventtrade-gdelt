# Session log — 2026-10-05, first build (branch dev, worktree gdelt-dev)

## Built

Nine components were built in parallel by separate agents, then integrated:

- adapters: `store.py` (SQLite, append-only trials), `gdelt_bq.py` (+ `queries/*.sql`, budget
  guard), `prices.py` (yfinance, Stooq), `universe.py` (ETF CSV, Wikipedia S&P lists).
- core: `aliases.py`, `features.py`, `labels.py`, `evaluate.py`, `cv.py`, `models.py`,
  `simulate.py`, `overfit.py`.
- experiments 0001 (BigQuery cost/timing) and 0002 (GKG org alias precision).

Integration (this log's author):

- `cli/main.py` (composition root) and `cli/pipelines.py` (one function per subcommand,
  Protocols `Store` and `GdeltSource`). Subcommands: status, init-db, load-universe,
  ingest-prices, ingest-gdelt, build-dataset, signal-study, backtest, overfit-report, holdout.
- Contract changes applied: `schemas.py` gained `VOL_LABEL_COLUMNS`, `SCORE_COLUMNS`,
  `OOS_COLUMNS` (moved from core/labels and core/models, which now import them), a pinned date
  dtype (datetime64[ns] midnight), `market` in `Asset.group`, corrected adjustment comment on
  `PRICE_COLUMNS`, units on `OverfitReport` and `BacktestStats`, and the return-date convention on
  `TRIAL_RETURN_COLUMNS`. `config.py` gained `horizons=(1, 5, 20)` and `seed`. `pyproject.toml`
  gained `lxml`, `tzdata`, `include_external_packages = true` (required by import-linter for the
  external forbidden modules; `google.cloud` had to become `google`), and a contract that
  `cli.pipelines` may not import adapters.
- Reconciled mismatches:
  - GKG global theme rows carry the theme in the metric name (`theme_ENV_OIL_docs` on
    `_GLOBAL`), but `map_to_tickers` maps entities, so ETF `gkg_themes` never matched. Added pure
    `core/features.themes_as_entities` (re-key to entity = theme, metric `theme_docs`/`theme_tone`).
  - `store.replace_labels` deleted every row of a horizon, so building Track B would wipe Track A
    labels. Now scoped to (horizon, ticker) in the frame.
  - Resumable ingest needed coverage by query: added `store.gdelt_session_dates(query_name, ...)`.
  - `realized_vol_label` names its column `value`, which collides with the feature's `value` in
    `evaluate._join`; pipelines renames it to `vol` before calling `daily_ic`.
  - B1 needs attention x tone: added pure `core/features.product_feature`.
- Docs: README (CLI contract, exit codes, resuming), `docs/domain.md`, `docs/components/cli.md`.

## Tried and rejected

- Importing `core.simulate` at module level in pipelines: every command (even `status`) paid
  ~35 s for the vectorbt/numba import. Now imported inside `_simulate`.
- Making `holdout` read pre-ingested hold-out data: the ingest commands refuse hold-out dates by
  design, so `holdout` fetches its own prices and (with `--execute`) GDELT, and keeps hold-out
  features in memory only (never in the `features` table).
- Putting signal results in `Trial.params`: the trial id is a hash of params + data window, so
  results go in `notes` (JSON: gate, sub-period ICs, SignalReport).

## Open issues

- Repo separation: the git common dir is `workspace/gdelt/.git` (own repo, branches main/dev),
  not gtm_system, and no file references gtm_system. The worktree directory itself and the repo
  still sit inside the `team_tool` folder tree; moving them out is the user's call.
- Nothing has been ingested yet: `prices`, `assets`, `gdelt_daily` are empty. The dry run for
  events 2024-01 estimated 2.01 GB. Full backfill does not fit one 900 GB month (exp. 0001):
  events + orgs + themes 2021-2025Q1 first, older themes later.
- Model gate (gate 2, CPCV model vs rule baseline) has no subcommand; `backtest --model` is
  walk-forward only. `holdout` supports rule trials only.
- DSR in `overfit-report` counts `backtest` trials only; signal-stage trials have no return
  series. Whether gate 3 should also inflate N by the signal trials is undecided.
- Gate-1 cost uses 2 x cost x turnover; a full long-short accounting would be ~4x (evaluate.py
  open issue). Not decided.
- `ingest-gdelt` skip rule: a month counts as done when its first and last session dates exist
  for that query name; theme/alias list changes are invisible to it.
- Track B features at full scale (~900 tickers x 2,600 days x ~8 features) were not timed; nor
  was Track A `build-dataset` on real data.
- A4 vol gate is a local variant (|t| >= 3 and sign in 2/3 sub-periods, no spread condition); the
  ADR does not define a gate for a volatility target.
- `ruff check` reports pre-existing findings in other agents' files under the user's global ruff
  rules (DTZ001 on deliberately naive datetimes in store tests, PLR0124 `v != v` NaN test).
- `lint-imports` crashes printing its banner on a GBK console; run with `PYTHONUTF8=1`.
- Scratch from component agents: `.venv/scratch/` and `data/scratch_aliases/` (gitignored).
- Experiment 0002's ~210 MB of BigQuery usage is not in `bq_usage` (raw client, before the adapter existed).

# Review fixes — 2026-10-05 (fixer)

Each finding was reproduced first; each fix has a regression test holding the triggering input.
Fast suite: 760 passed, 8 deselected (slow, adapters only, untouched). lint-imports: 6 kept.
No trials exist in the live store yet, so the stricter gates below change no logged verdict.

| # | Finding | Outcome | What changed |
|---|---|---|---|
| 1 | Model backtest chose inputs from full-dev-period gate-1 trials (selection look-ahead) | fixed | Reproduced: noise features picked by full-period t gave walk-forward IC t 4.47. Default `backtest --model` now takes every stored feature and selects per fold with `core/models.select_features` on that fold's training dates only (IC table from `evaluate.daily_ic`, which uses only date-t labels). `_passing_features` removed. Params record `feature_selection` (`per_fold_train_ic` or `user`); explicit `--features` prints a warning. Tests: `test_model_selection_uses_training_dates_only` (a feature that predicts only a fold's test block is not chosen for that fold), `test_model_default_ignores_signal_trials`, `test_select_features` case table. |
| 2 / 7 | DSR's N counted backtest columns only, not the signal trials that chose them | fixed | `overfit.deflated_sharpe` / `overfit_report` take `n_trials`; `pipelines.overfit_report` passes every logged non-hold-out trial of the track. When N exceeds the column count, V[SR] is floored at 1/(T-1) (null sampling variance), because one survivor column has no dispersion and would otherwise give SR_0 = 0. Tests: `test_n_trials_override_*`, `test_overfit_report_counts_all_stages`. |
| 3 | Model path built day-t cross-section from label availability (future bars) | fixed | Reproduced with the reviewer's demo (C dropped, B's z -0.707 -> -0.632). `models.to_design(..., keep_unlabelled=True)` keeps every featured row; `_model_scores` fits on labelled rows and scores all. `_weights` ranks only names with a price bar on the score date (known at t). Tests: `test_to_design_keep_unlabelled`, `test_model_scores_names_without_future_label`, `test_weights_need_a_bar_on_the_score_date`. |
| 4 | Gate 4 passed a CI entirely below zero | fixed | `_gate4_verdict`: pass only if CI low > 0 (and finite). Test: reviewer's input N(-0.001, 0.01), n=250, CI hi < 0 -> fail, plus a case table. |
| 5 | Gate-1 cost charged 2c*tau for a $1 long + $1 short spread | fixed | Cost is now 4 * cost_bps * min(1, h * turnover). Gate case table updated; reviewer's case (spread 15 bp, turnover 1, 5 bp) now fails. |
| 6 | NW default lags h-1 (Bartlett) oversized under overlapping labels | fixed | Reproduced: 5% test rejected ~11-14% at h=5/20. Default long-run variance is now max(Hansen-Hodrick with h-1 lags, Bartlett with max(auto, 2h) lags); explicit `lags=` keeps plain Bartlett. Monte Carlo (4000 reps, T=2500): P(|t|>=1.96) 0.048 / 0.049 / 0.059 and P(|t|>=3) 0.004 / 0.001 / 0.004 at h=1/5/20. Test: `test_nw_size_under_overlapping_labels` (old default kept as the triggering case). |
| 8 | `--by-country` theme rows reach no feature | fixed (by removal) | Reproduced (`{'USO': 2}`, EWJ nothing). `--by-country` removed from the CLI; `ingest_gdelt(by_country=True)` returns REFUSED. The adapter and SQL still exist for a future spec. |
| 9 | Country theme query relabels `_GLOBAL` rows, so plain gkg-themes re-pulls | fixed | Reproduced (three runs, three pulls). With the country query refused nothing overwrites `gkg_themes_daily`'s vintage. Test: `test_gkg_themes_by_country_refused_and_resume_holds` (False, True, False -> one pull; third run "1 already stored, 0 to pull"). |
| 10 | Outer team_tool repo does not ignore `workspace/gdelt/` or `.claude/worktrees/` | deferred | Confirmed: team_tool has no `.gitignore` and an empty `.git/info/exclude`. The fix is outside this worktree and touches the team_tool repo, so it is the user's call: move `workspace/gdelt` out of the team_tool tree, or add both paths to team_tool's `.git/info/exclude` (local, untracked, adds no reference to eventtrade in team_tool's history). |
| 11 | `holdout` without `--execute` fetched and wrote hold-out prices | fixed | Dry run now only reports and dry-runs GDELT estimates; prices are fetched only with `--execute`. Test: `test_holdout_dry_run_fetches_and_writes_nothing`. |
| 12 | Scratch scripts ran BigQuery outside `GdeltBigQuery` (not in `bq_usage`) | partly fixed | Confirmed (`data/scratch_aliases/bq.py`, `bq_diag.py`, `bq_urls.py`; `bq_diag.py` billed with no opt-in). All three now `raise SystemExit` on start, pointing at `adapters/queries/` + `GdeltBigQuery`. Not done: backfilling the past ~210 MB into `bq_usage` -- the exact billed bytes would need an INFORMATION_SCHEMA.JOBS lookup (itself a query), left to the user. |

Open after this pass:
- Model gate 2 (CPCV model vs rule baseline) still has no subcommand; per-fold selection is
  implemented for walk-forward only.
- `select_features` applies the IC and sub-period parts of gate 1, not the cost condition
  (turnover is not available per fold without the quantile machinery).
- A fold where no feature passes on its training dates scores nothing; on noise the model trial
  can therefore end with "no tradable weight dates" (MISSING) and is not logged.

# Gate-1 signal study + verification — 2026-10-05

Recorded in `docs/experiments/0003-signal-study-dev.md`. 96 signal trials logged (A 75, B 21);
these count toward the DSR N.

- Track A direction 0/42, Track B 0/21. A4 vol: 6/33 logged "pass" (massviolence and protest
  `share_lr.60`, h=1/5/20). Five read-only verification checks show all six are artefacts: a
  static between-ticker level (the lr transform is NaN on zero days, so it ranks tickers by
  sparsity, and SPY/TLT/IEF/SHY share an identical US feature) compared against a raw vol-level
  label. With ticker demeaning, a vol-surprise label or HAR controls, |t| <= 2.3. No leak was found.
  **Nothing is eligible for gate 2.** The 6 "pass" rows stay in the append-only trials table;
  0003 is the record that they are invalid.
- Must fix before any further trial (each change needs an ADR amendment, since it changes the
  test):
  - A4 gate uses a raw vol level (`pipelines._signal_study_vol`).
  - Gates take the sign from the observed t, not the pre-registration (`evaluate.passes_gate`,
    `_signal_study_vol`).
  - `lr` sparsity selection (`features.surprise`).
  - `z` std residue gives |z| up to 1.3e7 (`features.surprise`).
  - GKG attention divided by the number of aliases hit (`map_to_tickers` + mean).
  - Zero-mention days are NaN.
  - US-tied ETFs; `eaton` and `<ambiguous> inc` aliases.
- Open: A2 untested (no theme rows). Cost leg (4x, ~0.6 turnover) needs IC 0.03-0.05 at h=1/5,
  above the ADR prior. Decide whether that is intended.

# ADR 0002 implementation (features v2, gates, universe) — 2026-10-05

Three agents worked in parallel (features, gates, universe), then one integration pass. Nothing
is committed, no trial was logged and the SQLite db was not written.

- Features v2 (`core/features.py`, `FEATURES_VERSION = 2`): pseudo-count lr, z floor and clip,
  count zero-fill after first appearance, `org_docs` divided by the day total once, and the new
  `sm5` transform. `feature_name(spec, transform)` is now the only way names are built.
- Gates: `passes_gate(..., expected_sign=)` is required. A4 v2 uses `vol_surprise_label`,
  `vol_controls`, `partial_ic`, `placebo_feature` and `vol_gate`. Signal trial params carry
  `features_version` and `expected_sign`. sm5 features run at h = 5 and 20 only. H0's sign is
  None, and `backtest` refuses H0 unless `--sign` is given.
- Universe: TLT/IEF/SHY have no countries, there is one share class per company, `eaton` is
  ambiguous, and `nasdaq`/`gartner`/`morningstar` are dropped with any suffix.
- Integration: `load_universe` now deletes a reloaded track's tickers that are no longer listed
  (new `SqliteStore.delete_assets`; tests `test_delete_assets_removes_only_named_tickers` and
  `test_load_universe_drops_stale_rows_of_reloaded_track`). Added
  `schemas.VOL_CONTROL_COLUMNS` (and `labels.VOL_CONTROL_NAMES` is derived from it) and updated
  the vol label comment. Updated README CLI table, `docs/domain.md` and `docs/components/cli.md`,
  and added implementation notes to ADR 0002.
- Verified: the full fast suite (`pytest -q`) and `lint-imports` (6 contracts kept). Ruff is clean
  on changed files; the 10 remaining ruff errors are older ones in store.py, prices.py and
  test_store.py.

Next, in order: `load-universe` (removes GOOG/FOX/NWS and fixes TLT/IEF/SHY in the db),
`build-dataset --track A` and `--track B` (v2 features), then `signal-study`. Each v2 trial adds
to the DSR N.

Open decisions for the user, defaults recorded in ADR 0002's implementation notes:
- A4 sign in shock units (Goldstein expected -1) vs the literal +1 for every feature.
- sm5 on every z family (including A3 and A2) vs a narrower set, which means fewer trials.
- The store does not record the features version; `signal-study` treats "no sm5 feature" as a v1
  build. A meta table would make this exact.
- `gkg_orgs_daily.sql` still counts a doc naming two aliases of one ticker twice in the
  numerator. Fixing it needs per-ticker matching in SQL and a re-pull.
- `core/models.select_features` still takes the IC sign from the fold's data (model inputs). It
  needs the registered sign if it should match ADR 0002 item 3.
- TLT and IEF have identical `gkg_themes`, so they will produce identical A2 features once themes
  are pulled.

# ADR 0002 conformance review fixes — 2026-10-05

Fixer pass on two minor conformance findings. Nothing committed, no trial logged, db not written.

- **Backtest params had no `features_version`** (ADR 0002 item 5). Reproduced: rule and model
  backtest params lacked the key, so a v2 backtest hashed to the same trial id as the v1 one and
  would have been reported "identical trial already logged". Fixed in `pipelines.backtest`
  (both kinds; hold-out trials inherit it from their source params). Regression test
  `test_backtest_params_carry_features_version`: it failed with KeyError before the fix, and it
  checks that a v1-style backtest row already in the store does not block the v2 row. No
  backtest trial exists in the db yet, so no stored row is affected.
- **Alias drops wider than the ADR text.** Reproduced with `make_aliases` + `is_ambiguous`:
  NDAQ, IT and MORN keep no alias, and ETN loses `eaton` and `eaton co`. Kept the broader
  `_CITED_ANY_FORM` rule because the boilerplate carries every suffix, and the extra forms are
  1-50 dev docs each. Disclosed it in ADR 0002's implementation notes: NDAQ/IT/MORN leave Track
  B GKG, `eaton co` (2,993 docs) goes with `eaton`, and the notes say how to switch to the literal
  reading. Pinned `("eaton co", True)` in `AMBIGUOUS_CASES` and added it to ETN's must-drop set in
  `KEPT_CASES`.
- Verified: `pytest -q` 861 passed, 8 deselected; `lint-imports` 6 contracts kept; ruff clean on
  the changed files.
