# Agent log

Current session. Earlier sessions: see `index.md` (latest archive:
`archive/2026-10-06-intraday-foresight-sec.md`). Live state (row counts, BigQuery spend, trial
counts) comes from `eventtrade status` and `intraday-status`, not from this file.

## 2026-10-10 — handoff (convention §14.5)

**Where things are.**
- Branch `dev` of the standalone repo `workspace/gdelt`, worktree `.claude/worktrees/gdelt-dev`,
  pushed to the private `origin` (`github.com/Boyao0305/eventtrade-gdelt`).
- `main` holds only the initial commit; do not merge or push it without the user.
- Frozen checkout for trial-logging runs: `.claude/worktrees/gdelt-snap` (last at `5c9ab58`).
- Data lives in the main checkout (`EVENTTRADE_HOME=.../workspace/gdelt`):
  - `data/eventtrade.sqlite`: main store (assets, daily prices, GDELT daily, trials);
  - `data/intraday.sqlite`: Alpaca 15-minute bars and 15-minute GDELT;
  - `data/sec.sqlite`: SEC Form 4, 13D, submissions, CIK map.

**Research results (all pre-registered; see `docs/experiments/`).**

| Line | ADR → experiment | Verdict |
|---|---|---|
| GDELT daily signals, screens, event study, F1 | 0001–0006 → 0003–0007 | null |
| GDELT themes (A2) | 0004 → 0008 | null |
| GDELT intraday, fixed configuration | 0007 → 0009 | null (all means within ±2.3 bp) |
| GDELT intraday parameter search, 34 cells × 160 configurations | 0008 → 0010 | null (nothing beats costs) |
| Foresight, company and country, look-ahead diagnostic | 0009 → 0011 | company pre-news drift does not replicate; country underpowered |
| Foresight, country, first-firing freshness | 0010 → 0012 | null with power (0/154) |
| **SEC insider purchases and 13Ds** | **0011 → 0013** | **insider buys: +2% (all) to +3% (CEO/CFO, clusters) vs sector at 60 days, replicated 2016–19 and 2020–25, net of 30 bp; formally not confirmed (Holm: p 0.009 vs 0.0071).** 13D null |
| Insider purchases, fresh small/mid caps | 0012 → 0014 | partial; net and survivorship-stressed negative |
| Post-earnings drift (8-K 2.02, return-based surprise) | 0013 → 0015 | null (0/6 discovered) |
| Scheduled macro announcements on SPY (pre-FOMC, FOMC cycle, CPI/NFP/FOMC days, gaps) | 0014 → 0016 | null (0/5 discovered) |

- **Trials:** 8,182 total, 7,808 counting for DSR. The 374 look-ahead trials (O, O2, R) are
  excluded by `counts_for_dsr`.
- **Spend:** BigQuery ≈ $5.25 of the user's $20 cap. Everything since then is free (Alpaca,
  SEC).

**Next steps (the user delegated the choice and order of sources).**
1. **ADR 0012: replicate insider purchases on fresh stocks.**
   - Issuers outside the S&P 500/400, using the same SEC data already stored. Prices from
     yfinance into a separate store, hedged with IWM.
   - One or two tests (ins_all h60, ins_cxo h60), matched placebo, Holm.
   - Costs about 2 × 25 bp + 2 × 1 bp.
   - Survivorship bias is a known issue (current tickers only): state it.
   - Write the ADR, review it, then run once.
2. **ADR 0013: post-earnings drift.**
   - Announcement dates: from 8-K item 2.02 in `company_filings` (already stored).
   - Surprise proxy: the announcement-day abnormal return.
3. **If a cell is confirmed:** gates 2–3 (portfolio backtest, DSR/PBO), then the hold-out once.
   - The user turned Smart App Control off on 2026-10-10. The venv, scikit-learn and vectorbt
     work again: verified `import sklearn, vectorbt`.

**2026-10-10 (later) — ADR 0012 done (experiment 0014): "partial".**
- **Fresh small/mid caps** (6,764 / 2,698 / 1,137 events): insider buys beat matched comparable
  dates (p 0.0025), but the absolute 60-day effect is small (+0.4–0.9%). Only CEO/CFO is net
  positive (+0.14%).
- **Every cell is negative under the survivorship stress** (f_fail about 10%).
- **The insider line closes as a standalone strategy.** Trials: 8,185.
- **Next per the plan:** ADR 0013, post-earnings drift (dates from 8-K item 2.02 in
  `company_filings`; surprise proxy = announcement-day abnormal return).

**2026-10-11 — ADR 0013 done (experiment 0015): null.**
- Run once from frozen `cff10be`: 34,868 earnings days (8-K 2.02), 889 tickers. Timing share
  before d ≈ 0.23, so the window is close(d−2) → close(d+1). 38% of earnings have |z| ≥ 2.
- **Discovery: 0 of 6 cells at BH q 0.10** (matched p 0.36–0.89). There was no confirmation.
- Stocks after negative surprises *rise* (+1.3% at 60 days, the same as after positive ones).
  That is the survivor drift, which the matched null removes. Secondary big-move p values are
  low (earnings jumps revert less than other jumps), but they are reported only.
- Trials: 8,191 (6 P1). Spend unchanged (≈ $5.25 BigQuery; this study used no BigQuery).
- **Next (my choice, delegated):** single-stock event lines on the S&P 1500 are now exhausted on
  the free data (GDELT, insiders, 13D, earnings). Candidates not yet tested:
  - market-level scheduled macro events (FOMC/CPI surprise on SPY/sector ETFs; the user's
    original intuition);
  - other 8-K items (5.02 executive departures, 4.02 restatements) on stored `company_filings`.

  Both need a new ADR plus review before any data are touched.

**2026-10-11 (later) — ADR 0014 done (experiment 0016): null.**
- Run once from frozen `e7da840` on SPY 15-minute bars, with the calendar frozen (SHA-256 in the
  ADR).
- **0 of 5 discovered:**
  - pre-FOMC drift +2.6 bp vs placebo +12 bp;
  - FOMC cycle b +0.8 bp/day;
  - announcement days +9 vs +7 bp;
  - gap continuation and statement continuation null.
- Reported only: CPI/NFP days +16 bp, p 0.14. Power excludes only effects of about 25 bp or more.
- **Method fix found on synthetic data before the run:**
  - Placebo draws now include the events' own days (a permutation). The earlier pool-only draws
    were too narrow when events are a large share of a year × weekday.
  - ADR 0013's small calm-earnings pool had the same, anti-conservative issue. Its null verdict
    stands.
- **Data note:** Alpaca "regular-session" bars continue past 13:00 on NYSE half-days, so the 18
  half-days were dropped by rule.
- Trials: 8,196. Spend unchanged.
- **Next (my choice):** ADR 0015, other 8-K items on stored `company_filings`. Executive
  departures (5.02) and auditor changes or non-reliance (4.01/4.02) are negative-information
  events with a short-side prior. Same machinery as ADR 0013, with the permutation-style null.

**2026-10-11 (later) — ADR 0015, DoD contract awards: in progress (nothing matched to returns).**
- **Why this line:** 8-K distress items have too few events in the S&P 500/400 (e.g. 4.02:
  15 in 2016–19). In the small-cap store they are survivor-biased and hard to short. DoD
  publishes every award of $7.5M or more daily at 17:00 ET.
- **Done:**
  - ADR 0015 with review amendments (3 blockers).
  - `ingest-dod` (Wayback CDX + captures; defense.gov itself returns 403 to scripts):
    2,548 articles with status ok, ~250/yr for 2015–24, and 36,338 award paragraphs.
  - `map-dod`: title / former name / verified Exhibit 21 entry / division prefix, dated, with
    conflicts → ambiguous.
  - `ingest-dod-prices` (store `data/dodcap.sqlite` + full split history in `dod.sqlite`).
  - `ingest-sec-shares` (dei shares, us-gaap fallback, public float, profiles of unlisted
    CIKs).
  - `dod-study` (once-only, hypothesis D) with an e2e test.
- **Running:** the full `map-dod` (log `tmp/map.log`; names ~2 h, then Exhibit 21 search and
  documents ~4–6 h; resumable: rerun `bash tmp/et.sh map-dod`).
- **Remaining, in order (amendments):**
  1. Commit `docs/data/dod_issuer_map.csv`.
  2. Run `ingest-dod-prices`, then `ingest-sec-shares`.
  3. Amount audit (agent, 200 paragraphs, stratified by ratio; ratio uses market caps, no
     returns).
  4. Blind mapping audit (second agent, 150 paragraphs).
  5. Freeze: SHA-256 of the map, parser and audit in the ADR; commit.
  6. Run `dod-study` once from the frozen checkout.
- **Notes:**
  - The scraper agent opened two Aug/Sep 2025 DoD articles (hold-out period, no prices) while
    exploring HTML structure. They are not used.
  - My first defense.gov probe carried the user's email in its User-Agent. Non-SEC requests
    don't anymore.

**Open notes.**
- Holding-company CIK changes (DIS, CI, AVGO, LIN, …) lose pre-reorganization SEC filings. It is
  documented in ADR 0011; no fix is planned (ticker reuse risk).
- `act_13d` is about 50% activist (audit in ADR 0011): founders, private-equity sponsors and
  strategic stakes make up the rest.
- The scripts in the job tmp dir (`py.sh`, `et_snap.sh`, `run_*.sh`) are throwaway. `et_snap.sh`
  currently uses base CPython plus the venv site-packages; it can go back to the venv launcher.
