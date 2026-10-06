# Agent log

Current session. Earlier sessions: see `index.md`. Live state (row counts, BigQuery spend this
month, trial counts) comes from `eventtrade status`, not from this file.

## 2026-10-06 — handoff before context compaction

**Where things are.** Branch `dev` of the standalone repo `workspace/gdelt`, worktree
`.claude/worktrees/gdelt-dev`, pushed to the private remote `origin`
(`github.com/Boyao0305/eventtrade-gdelt`, default branch `dev`). `main` holds only the initial
commit; do not merge or push it without the user. Frozen checkout for trial-logging runs:
`.claude/worktrees/gdelt-snap` (last at `344d4bf`).

**Research result so far.** Every pre-registered hypothesis except A2 is tested and null:
v1/v2 gate 1 (experiments 0003–0004), ADR 0003 screen (0005), ADR 0005 extreme-shock event
study with nested optimization (0006), ADR 0006 F1 follow-up (0007, failed out of sample,
p 0.68). Trials in the store: 1,843. Hold-out (≥ 2025-04-01) untouched.

**In flight — A2 theme screen (ADR 0004), user approved on 2026-10-06.**
- Code ready (`screen --track A --set a2`); refuses until every dev month of `gkg_themes_daily`
  is stored. Stored: 2015-03 → 2017-08. October's free 1 TiB is nearly used (894.7 GB billed).
- The user connected a card and allowed spending, **cap $20 total without approval**. Price
  confirmed: $6.25/TiB after the free 1 TiB/month. Remaining pull ≈ 680 GB (estimate from
  experiment 0001; re-check with a dry run) → roughly $3 at current month-to-date usage.
- Next steps: (1) dry-run `ingest-gdelt gkg-themes --start 2015-03-02 --end 2025-03-31` for the
  exact estimate; (2) raise `bq_monthly_budget_bytes` for this run only as far as the $20 cap
  allows (there is no CLI flag yet — `cli/main.py` builds `GdeltBigQuery` from
  `cfg.bq_monthly_budget_bytes`; add a `--budget-gb` flag or an env override, with a test);
  (3) `--execute` the pull (single process, so the guard checks spend sequentially);
  (4) verify "0 to pull"; (5) refresh the frozen checkout and run `screen --track A --set a2`
  once; (6) write experiment 0008.
- Known latent issue (not used by A2): the v2 `_FAMILIES` theme family (`gkg.theme_docs.mean`)
  does not treat `theme_docs` as a day-total metric (`features.DAY_TOTAL_N_METRICS`); fix before
  any `build-dataset`/`signal-study` on themes.

**2026-10-06, after compaction — A2 pull started.** Dry run: 658.57 GB for 2017-09 → 2025-03
(month-to-date billed 894.67 GB) → ~1,553 GB this month, ~454 GB over the free 1 TiB ≈ $2.6.
Added `ingest-gdelt --budget-gb` (commit `074ff8e`; ceiling 4,600 GB = free tier + $20 cap) and
started three concurrent `--execute --budget-gb 1600` pulls over 2017-09→2020-06, 2020-07→2022-12,
2023-01→2025-03 (logs `pull_t{1,2,3}.log` in the job tmp dir). If interrupted, rerun the same
commands: stored months are skipped. Budget raised to 1,600 GB for this run only (worst case
≈ $2.9).

**2026-10-06 — A2 done, null (experiment 0008).** All three pulls finished. "0 to pull" was
verified (121/121 months); October billed 1,553.29 GB ≈ $2.6 over the free tier. Frozen checkout
moved to `074ff8e`; `screen --track A --set a2` ran once: joint p_S 0.200, p_M 0.117 → stops;
8 trials, store total 1,851. **Every ADR 0001 hypothesis is now tested; none passed.** No further
trial-logging run without a new ADR and the user's input on direction (intraday data, new
sources).
- Open: `test_screen_confirms_only_discovered_cells` failed once in a `-k "screen or theme or a2"`
  run (EB `shrunk_ic` None for a non-commodity cell), then passed 4 times (alone, whole file,
  same subset twice); pairwise bisect found no leaking test. Treat as a flake to investigate. It
  cannot change a verdict (EB feeds notes, not the BH discovered set).
- Fixed on dev after the run: the dry run warned "estimate exceeds what is left" for a 0 GB
  estimate when the month was over budget.

**Open question from the user, answered on 2026-10-06 (see the final report): intraday horizons (hours/minutes).** Not tested.
Points for the answer: GDELT updates every 15 minutes and `DATEADDED` has 15-minute resolution,
so hour-scale is the finest meaningful horizon (minutes are below GDELT's own latency and belong
to newswire feeds); free intraday history is short (yfinance 1-minute ≈ 30 days, 1-hour ≈ 730
days), which would fall mostly inside the hold-out; a usable dev-period test needs a historical
intraday source (e.g. Alpaca's free IEX minute bars back to ~2016, needs an account/API key from
the user, or paid Polygon/Databento). Any intraday test needs its own pre-registration (ADR 0007).

**Rejected / lessons this session** (details in the archive): Storage API (403), parallel
heavy writes, background test runs under memory pressure, stationary-bootstrap max-t with
block 40 on sparse grids (2× oversized → placebo-date null), spaced confirmation draws (hang and
oversize → distinct unspaced draws).
