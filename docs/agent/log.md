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

**Open notes.**
- Holding-company CIK changes (DIS, CI, AVGO, LIN, …) lose pre-reorganization SEC filings. It is
  documented in ADR 0011; no fix is planned (ticker reuse risk).
- `act_13d` is about 50% activist (audit in ADR 0011): founders, private-equity sponsors and
  strategic stakes make up the rest.
- The scripts in the job tmp dir (`py.sh`, `et_snap.sh`, `run_*.sh`) are throwaway. `et_snap.sh`
  currently uses base CPython plus the venv site-packages; it can go back to the venv launcher.
