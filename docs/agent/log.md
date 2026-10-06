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

## 2026-10-05 (night) — resume point, autonomous run

The user is away and asked me to continue autonomously as the researcher, documenting as I go.

**Done, in order:** v2 committed (`394a717`, 861 tests, 6 import contracts). Look-ahead review of
the v2 code re-run separately (the workflow's reviewer had returned empty): 20 perturbation runs
bit-identical for dates <= t, labels/controls/partial IC/placebo clean, no hold-out reads. One
float-residue nit in `evaluate.placebo_feature` fixed (shift before cumsum) with regression test
`test_placebo_day_t_value_leaves_no_float_residue`. Universes reloaded with v2 rules (A 63 ETFs,
B 900 stocks: GOOG/FOX/NWS removed). ADR 0003 (sub-category screen) written BEFORE any v2 trial.

**Next steps (in this order):**
1. `build-dataset` A then B with v2 definitions (sequential; concurrent builds lock SQLite).
2. `signal-study` A and B (v2 trials get new ids; v1's 96 stay).
3. If a v2 signal passes gate 1: adversarial verification (static/placebo, vol clustering,
   definitions) before gate 2.
4. If v2 is null (or after step 3): run ADR 0003 — needs GICS sector per stock in the universe
   adapter, quad/sanctions/violence families as sm5 features, the bootstrap joint test,
   empirical-Bayes shrinkage, BH-FDR, discovery 2015-2019 / confirmation 2020-2025Q1.
5. Write experiment 0004 (v2 results) and 0005 (screen) as each completes.

**Rules I am holding to:** hold-out untouched; no merge of dev into main without the user; every
evaluation logged as a trial; BigQuery budget unchanged (no new pulls needed for 0003).

## 2026-10-05 (night) — ADR 0003 screen integrated (`eventtrade screen`)

Three parallel agents built the parts; this pass wired them into `cli/pipelines.screen` and the
`screen --track A|B [--n-boot N]` subcommand. Nothing committed, no CLI command run, the db not
written (a v2 `build-dataset` was running against it).

- **Parts.** `Asset.screen_group` + `assets.screen_group` migration + universe groups (A: dm 20,
  em 21, commodity 7 incl. XLE; B: GICS sector from Wikipedia). `core/screen_features`: the S3
  signals (`sm5_z.60` of each family's `mean`). `core/screen`: cell IC, joint bootstrap test, EB,
  BH, Holm, confirmation.
- **Orchestration choices fixed before any screen result** (none exists yet):
  joint test significant when min(p_S, p_M) < 0.025 (Bonferroni over the two statistics; ADR
  0003 names both but no combination rule); stationary-bootstrap mean block 40 (2 x the longest
  horizon); seed = config seed; discovery labels embargoed so an h-day label ends by
  2019-12-31; EB on directional ICs (registered sign, or the discovery-t sign of two-sided cells)
  pooled toward their mean; BH on the bootstrap per-cell p-values; confirmation spread = mean
  quantile spread and turnover over the confirmation period within the group, cost as gate 1.
  Trial windows are the fixed period bounds (not the data's min/max), so ids are stable.
- **Thin groups** (< 5 names with a signal on more than half the discovery days) are skipped and
  printed, and listed in every trial's notes; their cells are not logged as trials.
- **Labels:** stored dev labels at h = 5, 20 are reused when they cover every priced ticker,
  otherwise built from dev prices in memory.
- Tests: `tests/cli/test_pipelines.py` (per-cell/period logging, rerun logs nothing, stop path,
  confirmation only for discovered cells, no read ends after the hold-out start, Track B end to
  end with the real bootstrap) and `tests/cli/test_main.py` (exit 4 without screen groups).

**Before running the screen:** the v2 build must finish; then `load-universe` (both tracks) so
`init()` adds `screen_group` to the live db and the upsert fills it; `screen` exits 4 until then.
Open for the ADR owner: the S/M combination rule and directional S/M reading, the MSCI
inception calls (ARGT, VNM, KSA, QAT, UAE as em; EIS em and GREK dm by the inception rule),
Track A share = family count / the country's n_events, Track B org_neg_docs as the `mean`
(negative / mentioning docs).

## 2026-10-05 (late night) — ADR 0003 implementation notes applied to the screen code

Tests first, then code. Nothing committed; no CLI command run against the real db. The open
questions above are settled by ADR 0003's implementation notes.

- **Trial identity.** Every screen trial's params now include `n_boot`, `mean_block`, `seed` and
  `members_hash` (first 16 hex digits of sha256 of the cell's sorted tickers joined by `,`).
  `screen` returns REFUSED (2) for n_boot < 999 (`SCREEN_MIN_N_BOOT`); the CLI rejects
  `--n-boot` < 999 as a usage error (exit 2).
- **Every cell counts.** The skip-group rule (`_thin_groups`) is gone. A cell is thin when its
  discovery IC (>= 5 names with signal and label) exists on fewer than half of its embargoed
  discovery trading days (`_is_thin`). Thin cells are left out of the joint test and EB, enter
  BH with p = NaN (counted in m, never discovered) and are logged with
  `note: "insufficient names"`. Rejected: keeping a group-level rule (the notes speak of cells).
- **Shrinkage.** New `core/screen.shrink_directional`: signed cells pool sign x IC, two-sided
  cells pool their unfolded IC, the direction is applied after shrinkage. The previous code
  folded two-sided cells (direction x IC) before pooling, which the notes rule out. Trial notes
  carry `shrunk_ic` (directional) and `eb_posterior` (the pooled value's posterior).
- **Joint test.** Behaviour unchanged (z = sign x t, |t| two-sided; S, M; min(p_S, p_M) < 0.025);
  the module docstring and pipeline comment now cite the notes.
- **Universe.** `screen_group` = MSCI class as of 2015-03-02: EIS em -> dm, GREK dm -> em;
  ARGT, VNM, KSA, QAT, UAE stay em; every other country class checked and unchanged (none was
  reclassified between its fund's inception and 2015-03-02). `msci_note` rewritten to state the
  2015-03-02 class. Group sizes unchanged (dm 20, em 21, commodity 7).
- Screen pipeline tests now use n_boot = 999 (about 10-25 s each instead of 4-7 s).

## 2026-10-05 — Review finding: confirmation p-value size at h = 20 (deferred)

Finding (statistics, minor): `screen.confirm` takes p = norm.sf(sign * t) on the HAC t, and at
h = 20 that test rejects more often than its nominal level, so Holm's 5% FWER is not held
for h = 20 cells.

- **Reproduced.** I simulated 30,000 MA(h-1) null series at T = 1320, computed t with
  `screen._nw_t_rows` (seed 7) and counted rejections at alpha 0.05 / 0.025 / 0.0125 / 0.01.
  h = 5: sd(t) 1.003, rates 0.0493 / 0.0248 / 0.0130 / 0.0106, so it is calibrated.
  h = 20: sd(t) 1.029, rates 0.0546 / 0.0300 / 0.0164 / 0.0134, which is 1.09x to 1.34x
  nominal, and the excess grows at Holm's smaller levels. The reviewer's 60k run found a
  slightly larger excess (1.14x to 1.44x). Both runs show the same direction.
- **Deferred, rule not changed.** ADR 0003's implementation notes register "Confirmation
  p-values use the normal approximation of the HAC t". Calibrating the test would change a
  registered rule, whether by a pre-simulated null table, a Student-t with df about 30, or the
  seeded stationary bootstrap. Any of these needs an addendum to the notes from the ADR owner,
  written before any confirmation run. Until then, read an h = 20 confirmation near the Holm
  threshold as weaker evidence than its nominal level suggests.
- The only code change is a sentence in the `core/screen.py` module docstring that states the
  known size loss. Behaviour is unchanged. Fast suite: 1069 passed, 8 deselected.
  lint-imports: 6 contracts kept.

## 2026-10-05 (late night) — resume point 2

**Results so far (all on the dev period; hold-out untouched):**
- v2 gate-1 study: 0 passes on both tracks (experiment 0004). The A4 placebo caught the static
  country-risk effect that fooled v1.
- ADR 0003 sub-category screen: joint test not significant on either track (A p_S 0.116 / p_M 0.260
  over 42 cells; B 0.329 / 0.536 over 66 cells), so both stopped at step 2 (experiment 0005).
- Trials logged: 338 (v1 96, v2 134, screen 108).

**In flight:** `ingest-gdelt gkg-themes --start 2015-03-02 --end 2025-03-31 --execute` started
2026-10-05; it stops when October's budget runs out (~362 GB left at start) and must be re-run in
November (resumable; it skips stored months). Check with `ingest-gdelt gkg-themes ...` without
`--execute`: "0 to pull" means complete.

**Next:** implement the ADR 0004 A2 theme screen (8 cells) now, but run it only when the theme
pull is complete for the whole dev period. Do not read theme values before that run.
After A2, every ADR 0001 hypothesis has been tested; write the final summary experiment.

## 2026-10-05 — ADR 0004 A2 theme screen implemented (not run)

The GKG theme pull was running throughout. No theme value was read: the work used only the SQL
contract, the code and synthetic fixtures. I did not even run a `SELECT DISTINCT metric` against
the live db, because a long read could block the running writer.

- **Generalised rather than duplicated.** `screen(..., screen_set="s3"|"a2")` keeps one code path.
  `a2` changes only the signals (`screen_features.THEME_SIGNALS`), the groups (pinned
  `THEME_GROUP_MEMBERS`), the GDELT read (gkg `_GLOBAL` rows), the hypothesis (`A2`) and two
  extra trial params (`hypothesis`, `themes_hash`). CLI: `screen --track A --set a2`; the default
  `s3` is unchanged. S3 trial ids are pinned by literal ids computed before the change
  (`test_s3_trial_ids_unchanged`).
- **Theme signals** (`theme_inputs`, `theme_signals`): per ETF, attention = Σ mentions of mapped
  themes / day total, and tone = Σ tone / Σ mentions, keyed as entity `a2:<ticker>` and pushed
  through `build_features`. This counts theme mentions, not distinct documents; the query has
  per-theme counts only. The choice is recorded in ADR 0004's implementation notes.
- **Run condition:** `pending_months(store, query, start, end)` was factored out of
  `ingest_gdelt`. `screen --set a2` exits 4 before any GDELT read unless it is empty for the whole
  dev period. The test checks that no `read_gdelt_daily` call happens on refusal.
- **Found, not changed:** the v2 `gkg.theme_docs.mean` family (A2 in `_FAMILIES`) sums n (the day
  total) over an ETF's themes, because `theme_docs` is not in `features.DAY_TOTAL_N_METRICS`. Its
  value is therefore Σ docs / (k_present x total). No v2 theme trial exists yet (themes were never
  pulled), and the A2 screen does not use this path. Fixing it would change features v2, so the
  decision is left to the owner.
- ADR 0004 says "19 ETFs", but its lists hold 18 distinct tickers (XLE is listed twice). The notes
  record this.
- Tests: `tests/core/test_theme_features.py` (sign table verbatim, groups vs CSV, frozen theme
  mapping, case table, weekend roll, hand-reference sm5, no-future-rows perturbation, day-t
  reach) and the A2 orchestration and CLI tests in `tests/cli/`.
- **Next:** when `ingest-gdelt gkg-themes` (dry run) reports 0 to pull, run
  `screen --track A --set a2` from a frozen checkout, then write the experiment.

## 2026-10-05 — ADR 0005 event study wired into the CLI (not run)

No event-study value was computed on real data. Only synthetic fixtures were used, and the
command was never run against the real db.

- `core/event_signals.py` (cells, grid, shock scores, outcomes) and `core/eventstudy.py`
  (triggers, event-date series, max-t bootstrap, costs) were written by two agents. Integration
  is `pipelines.event_study` and the CLI command `event-study --track A|B [--n-boot N>=999]`.
- **BH spans both tracks.** ADR 0005 runs BH across all 47 cells, while the command runs one
  track at a time. A run therefore reads the other track's cell p-values from its logged
  discovery trials (same n_boot, block, seed and features version). Until those exist, the run
  stops after discovery. Holm for confirmation likewise spans the discovered cells of both
  tracks and is reported as pending until the other track's confirmations are logged. The
  sequence is A, B, A (or B, A, B); the result does not depend on the order.
- **Judgement call, E4/E4p:** declustering runs on the attention signal, and the declustered
  events are then split by same-day tone z (ADR: "the event is split"). E4 and E4p therefore
  partition E5's events. Splitting first would let a positive spike fire 5 days after a negative
  one. Pinned by `SPLIT_CASES` in `tests/cli/test_event_study.py`.
- **Deviations carried over from the core modules (see their docstrings):** event-date
  portfolio instead of the ADR's daily calendar-time portfolio; declustering blocks p+1..p+20;
  baseline min_periods W // 3. All three need an ADR 0005 implementation note from the owner.
- Speed on the synthetic fixture (13 ETFs, 910 days): about 2 min for one Track A run with the
  full grid. Track B at full scale (about 900 stocks) has not been measured.
- **Next:** add the ADR 0005 implementation notes, then from a frozen checkout run
  `event-study --track A`, `--track B`, then `--track A` again, and write the experiment.

## 2026-10-05 — ADR 0005 review findings: fixes and deferrals (still not run on real data)

Nine review findings on the event-study code were reproduced on synthetic data or with
read-only metadata. Five are fixed with regression tests. Four are deferred because the fix
would change a rule that ADR 0005 pre-registers. No event-study value was computed on the real
db, and no trial was logged.

Fixed:
- **Calendar-time portfolio (conformance).** `eventstudy.calendar_time_series` now implements
  the ADR's form, replacing the event-date series. An event dated t is active on trading days
  t+1..t+h. Each active day's value is the mean h-day outcome of its active events. It now
  takes `trading_days`. The earlier deviation note no longer applies.
- **Holm verdict never logged for the first track to confirm.** Trials are append-only,
  enforced by DB triggers, so they cannot be updated. Withholding the pending trial would
  deadlock: each track needs the other's logged confirmation p. Instead, the run that
  completes Holm writes `holm_verdicts` into its confirmation notes: trial id, p, Holm reject
  and confirmed, for every discovered cell of both tracks. `pipelines.event_verdicts(store,
  n_boot, seed)` resolves a pending trial from that record. A, B, A is enough, and a fourth run
  changes nothing. Pinned in `test_holm_waits_for_the_other_tracks_confirmation`.
- **Confirmation p.** The normal p of the HAC t is replaced by `eventstudy.placebo_p`. Each
  event date, with all its tickers, moves to a random date of the same embargoed window,
  spaced 21 apart when feasible, and the directional t is recomputed (n_boot draws, seed =
  config seed). With clustered events (4 tickers on 8 days, h = 20, 300 reps), the normal p
  rejected 12.7% and the placebo p 7.7% at a nominal 5%. With 3 tickers on 11 days it was
  12.3% vs 8.3% (an independent 300-rep rerun of the placebo gave 6.3%; Monte Carlo se is
  about 1.3%). Spread-out events gave 4-8% for both. The placebo p is a randomisation test,
  exact when event dates are exchangeable with the placebo dates. ADR 0005 does not fix the
  confirmation test.
- **Abnormal-vol overnight leak.** The forward vol is now open-to-open over opens t+1..t+1+h,
  from entry; the trailing leg is unchanged. A +5% close t -> open t+1 gap no longer moves
  abnormal vol at t, as `test_pre_entry_overnight_gap_does_not_enter_abnormal_vol` pins.
- **Level vs crossing.** Behaviour is kept: the level rule plus the 20-day block. It is now a
  judgement call in the `core/eventstudy` case table, pinned by trigger cases.

Deferred (the ADR owner must amend ADR 0005 before any run):
- **Max-t bootstrap oversized on short grids (critical).** Reproduced on a single config with
  iid data and block 40: 13-15% rejection at 0.05 for 40-100 dates. Block 40 and the bootstrap
  are registered. Suggested amendment: a placebo-date null for the search correction (the
  32-configuration analogue of `placebo_p`), or a block scaled to the grid. The calendar-time
  change lengthens the grids (active days instead of event days). The reviewer's realistic
  null (20 DM-like tickers, all 32 configs, 200 reps, n_boot 199) now rejects 3.0% / 6.5% /
  10.0% at 0.01 / 0.05 / 0.10, against 2.25% / 9.5% / 15.5% with the event-date series.
  Sparse cells such as E2 commodity (few event dates) are still exposed, so the amendment is
  still needed.
- **Sufficiency counts events, not event days.** The ADR says "30 events". The calendar-time
  form removes the degenerate n = 13 NW t, but clustered cells still have few independent
  dates. Suggested amendment: also require >= 30 event days.
- **Abnormal vol median bias at h = 1.** Still -0.17 to -0.21 under the null after the
  forward-leg change. "Group's median" is registered. Suggested amendment: group mean, or drop
  h = 1 for vol cells. The placebo confirmation p absorbs the bias; the `directional_mean > 0`
  gate does not.
- **E2 raw-return drift.** "Raw return, absolute direction" is registered. The placebo
  confirmation absorbs drift; discovery does not.

Also pending for the owner: an ADR 0005 implementation note recording the level rule, the
active window t+1..t+h, the placebo confirmation p, the open-to-open forward vol and
`holm_verdicts`.

## 2026-10-05 — ADR 0005 Amendment implemented: placebo-date discovery null, event-date sufficiency (not run)

No event-study statistic on real data was computed or inspected. The real db was opened
read-only (`mode=ro`, a store subclass that refuses writes) only to time the discovery. The
harness printed timings and sizes, never a mean, t or p. Nothing was logged.

- **Discovery p = placebo-date max-t** (`eventstudy.max_t_placebo`, replacing
  `max_t_bootstrap`, which is deleted along with its tests; nothing else used it). Observed M is
  the max directional t of the exact calendar-time series over the cell's sufficient
  configurations. Each of n_boot draws (seed = config seed) takes one uniformly random
  permutation of the trading days of the cell's embargoed discovery window and moves every event
  date of every configuration, with all its tickers, to its image. Then every configuration's
  calendar-time NW t is recomputed and the maximum taken; p = (1 + #{M* >= M}) / (n_boot + 1).
  Pinned against a loop over `calendar_time_series` (same permutations) in
  `tests/core/test_eventstudy.py`.
- **Judgement calls** (in the module's case table):
  - One permutation per draw is shared by the whole cell, not one per (θ, W) trigger set. The
    Amendment requires that the h's of a trigger set share the mapping, and this does that.
    Sharing it across trigger sets as well keeps the θ/W correlation of the max. Independent
    mappings would inflate the null max and make the test conservative.
  - Dates are distinct only, with no 21-day spacing, following the Amendment's wording.
    `placebo_p` (confirmation) keeps its spacing.
  - The placebo window is the union of the configurations' embargoed windows (the h = 1
    window). A configuration's outcome is read only inside its own window, so an event moved
    past a longer h's embargo (at most 19 of about 1,220 dates) drops out of that draw.
  - The units moved are the events with an outcome on their own date.
- **Size, synthetic**: 4 tickers, 2 nested trigger sets x h 1/5/20, ~32 clustered event dates,
  common drift 0.15 per day. Over 300 reps the rejection rates at 0.01 / 0.05 / 0.10 were
  0.3% / 4.3% / 10.0%, while the naive max t was "significant" in more than half the reps.
  The test pins 150 reps within 1-10%.
- **Sufficiency** = `eventstudy.sufficient`: >= 30 events and >= 30 distinct event dates
  (`MIN_EVENT_DAYS`). Both counts and both minima are in every trial's notes. The rule also
  applies to the confirmation evaluation, as "in the period" reads.
- **Trial identity**: params drop `mean_block` and add `null: "placebo_dates"`
  (`pipelines._event_settings`, also used to match the other track's logged trials and by
  `event_verdicts`). No id can equal one from the bootstrap design, which a test pins.
  Discovery notes' `search` carries `test: placebo_dates_max_t`, `n_draws`, `n_dates` and
  `null_max_mean`.
- Confirmation is unchanged (`placebo_p` on the confirmation window).
- **Speed**: per configuration, a (event date x window date) table of the tickers' outcome
  sums and counts is built once (one matmul). A draw is then a gather plus a cumulative-sum
  window, and `_nw_t_rows` handles all draws at once. `_calendar` no longer re-iterates a
  DatetimeIndex element by element, and the observed series is no longer joined twice. Real-data
  timing (n_boot 999, read-only, dev period):
  - **Track A**: about 20 min. Read 175 s, scores and outcomes 25 s, then per cell 13-39 s:
    E1|quad4|em 17 s, E2|violence|commodity 13 s, E3|quad4|em 39 s; 44 cells x 23 s mean.
  - **Track B**: about 14 min. Read 214 s, scores and outcomes 209 s, then E5 169 s (17.8k
    events max, 1,193 event dates) and E4 126 s; 3 cells x 147 s.
  - Not included: trial writes (about 1,400 rows, a few seconds) and confirmation (one
    `placebo_p` per discovered cell).
  - A first Track B timing attempt died during the read without a traceback. The rerun
    completed normally.
- The fixture suite (`tests/cli/test_event_study.py`) now runs the real placebo max-t end to
  end and spies on `max_t_placebo`'s inputs: only sufficient configurations, discovery windows,
  n_boot draws, seed.
- **Next:** from a frozen checkout, run `event-study --track A`, then `--track B`, then
  `--track A`, and write the experiment.

## 2026-10-05 — Confirmation placebo p: sampler hang and date spacing fixed (not run)

Two review findings on `eventstudy.placebo_p`, both reproduced on synthetic data. No
event-study value was computed on the real db, nothing was logged, and no confirmation has been
run, so no trial carries the old construction.

- **Sampler hang (critical, fixed).** `_spaced_draws` redrew a whole row of k integers until all
  k were distinct. That loop never returns once k is around sqrt(free) or more. Reproduced with
  n = 1,320, gap 20, 999 draws and a 25 s limit: k = 30 finished, k = 58 and 150 did not. The
  sampler is now `_distinct_draws`: each row is the first k of a uniformly random permutation
  (argsort of uniform keys, chunked). That is the same uniform k-subset law with no rejection
  loop, and k = 30 to 1,320 now take under 0.1 s. Synthetic `placebo_p` timing (999 draws,
  h = 20, 1,320 days): 0.2 s for 3 tickers / 57 dates, 0.4 s for 10 / 180, and 37 s for an
  E5-sized cell (400 tickers, 1,193 dates, 17.5k events). Pinned by
  `test_distinct_draws_terminate_and_are_distinct` (n = 1,320, k in {58, 150, 300, 1,320},
  with a time bound) and `test_distinct_draws_are_uniform_and_unspaced`.
- **Placebo dates spaced 21 apart (major, fixed).** Declustering is per (ticker, signal), so a
  cell's pooled event dates are not spaced, but the confirmation null spaced its placebo dates
  21 apart whenever that fit (about 63 dates or fewer on 1,320 days). Reproduced with 2 signals
  x 3 ETFs (each signal declustered, firing for all 3 tickers), h = 20, drift, 58 pooled dates,
  199 draws, 1,000 reps: **8.5% / 14.3%** rejected at 5% / 10% with spacing vs **3.8% / 10.1%**
  without. `placebo_p` now draws distinct, unspaced dates, which is the discovery null's
  construction (`max_t_placebo`). Its `decluster_days` argument and the `PlaceboResult` field
  are removed, and the notes record `placebo_spacing: 0`. Triggers still decluster at 20 days.
  Pinned by `test_placebo_p_size_at_h20_with_two_signals_whose_pooled_dates_are_not_spaced`
  (200 reps, within 1-8%).
  - **Implementation note for the owner.** This follows the Amendment's "same placebo-date
    construction" as the discovery null (uniform distinct dates). The Amendment also says "as
    already implemented by the fixer (`eventstudy.placebo_p`)", and that implementation spaced
    its dates. Spacing was a judgement call in the module's table and is not in the ADR text.
    It is dropped before any confirmation was computed. If the owner reads the Amendment as
    pinning the spaced version, it needs a one-line ADR note either way.
  - `test_placebo_p_absorbs_a_common_drift` now checks 8 panels (median z > 3, median p > 0.2,
    every null mean > 2) instead of one fixture. The new event generator draws from a
    different random stream, and the old single fixture landed at z = 2.0.

## 2026-10-06 — resume point 3

- A2 theme screen committed (`1ebd019`), not runnable until the GKG theme pull is complete
  (stored through 2017-08; October budget exhausted at 894.7/900 GB; resume in November or after
  the user enables billing — the user was asked, no answer yet).
- ADR 0005 (extreme-shock event study, 47 cells x 32 configurations, nested optimization)
  pre-registered (`487564c`), amended before any result (`73229b8`: placebo-date discovery null,
  >= 30 events and >= 30 event dates). Implementation committed `ba718c2` after review; simulated
  size 2.4-4.7% at 5%; a hanging confirmation sampler and an oversized sparse confirmation were
  fixed.
- In flight: `event-study` A -> B -> A from the frozen checkout `.claude/worktrees/gdelt-snap`
  at `ba718c2` (script `tmp/run_es.sh`, logs `tmp/es_*.log`). Rerunning is safe: trial ids are
  deterministic and logged trials are skipped.
- Open questions to the user: push the repo to a private GitHub remote for backup (it has no
  remote); pay ~$5 for the remaining theme pull or wait for November's free tier. Advised against
  creating extra Google accounts to multiply the free tier (against Google's terms).
- Next: experiment 0006 from the event-study output; if any cell is confirmed, gate 2/3 as a
  combined event portfolio, then the hold-out once.

## 2026-10-06 — ADR 0006 follow-up F1 implemented (not run)

- `event_signals.composite_events`: union of the per-signal fire conditions (family z >= theta,
  tone z <= -theta) as the per-day max directional score, then ONE `eventstudy.triggers`
  declustering per ETF. Case table (15 cases) + a random-panel cross-check against `triggers`.
- `pipelines.followup_f1` / CLI `followup --id F1 [--n-boot N>=999]`: reuses `shock_scores`
  (W = 60, EM assets only), `outcomes` (abnormal vol, h = 20, EM group median),
  `_event_eval` (window + embargo, calendar-time series, NW t), `_event_confirm_test`
  (`placebo_p`, one-sided +), `_event_settings`, `_members_hash`, `_log_trial`. One trial,
  hypothesis `F1`, data window 2020-01-01..2025-03-31. The trial id is computed before any
  data is read; if it is logged, the rerun prints the stored verdict and reads nothing.
- Judgement calls (not in ADR 0006 text): declustering runs over the whole dev period and the
  events are then windowed, as ADR 0005's confirmation does (an event late in 2019 can block
  early-2020 dates); an inconclusive result is still logged as the one trial (verdict
  `inconclusive`, p null) since the test was run.
- Tests only on synthetic stores (`tests/cli/test_followup.py`): single trial with every fixed
  setting, rerun idempotent and not recomputed, hold-out never read, refusal for n_boot < 999
  (pipeline and CLI), inconclusive path, verdict case table. No statistic was computed on real
  data; the real run must be done from a frozen checkout (CLAUDE.md).

## 2026-10-06 — F1 review fixes

- **Fixed (major): F1 could be logged more than once.** The once-only guard matched the exact
  trial id, which includes n_boot, seed, features_version and members_hash, so
  `followup --id F1 --n-boot 1000` after a 999 run recomputed the test and logged a second F1
  trial. I reproduced this on the synthetic fixture: 2 F1 trials. Fix: `followup_f1` now checks for
  ANY trial with hypothesis `F1` before reading assets, prices or GDELT. If one exists, it prints
  the first (stored) verdict with its own n_boot/seed, warns if more than one exists, and logs
  nothing. n_boot is pinned: there is a new constant `F1_N_BOOT = 2000`, `followup_f1` has no
  n_boot parameter, and the CLI has no `--n-boot` (passing it exits 2). Regression tests in
  `tests/cli/test_followup.py`: a second run after changing n_boot, seed, features version or EM
  members logs nothing and reads nothing (the reads are booby-trapped). There are also tests that
  any `--n-boot` is a CLI usage error and that `F1_N_BOOT == 2000`. Tests monkeypatch
  `F1_N_BOOT` to 999 for speed.
- **Fixed (minor, docs only): ADR 0006 miscounted the modal-selection cells.** A read-only query
  of the logged 2015–2019 discovery trials (selected configs only, no statistic computed) gave
  8 EM E3 cells: 3/60/20 x4 (quad1, quad2, root_14, tone), 2.5/60/20 x3 (quad4, root_163,
  violence), 3/120/20 x1 (quad3). The modal configuration is still 3/60/20, so the code is
  unchanged. I added a dated Amendment to ADR 0006 that corrects the description and states that
  no rule changes. It also notes the hypothesis-level once-only guard.
- Docs updated: README `followup` row, `docs/components/cli.md`. Verification: full fast suite
  1275 passed (8 slow deselected), lint-imports 6/6 contracts kept, and ruff is clean on the
  touched files. The 10 remaining ruff findings are in untouched files
  (`adapters/prices.py`, `adapters/store.py`, `tests/adapters/test_store.py`).

## 2026-10-06 — resume point 4

- ADR 0005 event study: null on all 47 cells (experiment 0006, 1,504 trials).
- ADR 0006 F1 follow-up (data-suggested EM conflict -> abnormal vol): run once from frozen
  checkout `344d4bf` after a verified suite (1,275 passed, run in foreground chunks because a
  background run was reaped for low memory). Result: fail, mean -0.0051, t -0.57, p 0.678
  (experiment 0007). F1 can never be re-run (guard on hypothesis).
- Trials in the store: 1,843. Every pre-registered hypothesis except A2 is tested and null.
- Remaining: A2 theme screen, runnable once the GKG theme pull completes (Nov free tier, or
  ~$5 with billing — user's call, still open). Open question also: private GitHub backup.
- Lesson: on this machine, run the full suite in foreground chunks (<10 min each); long
  background shells get reaped under memory pressure.
