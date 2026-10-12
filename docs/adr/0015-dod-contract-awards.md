# 0015 — Department of Defense contract awards (hypothesis family D)

Date: 2026-10-11. Written before any award was matched to prices. The user delegated the choice
and order of event sources. Experiments 0003–0016 found no tradable effect in GDELT news,
insider/13D filings, earnings surprises or scheduled macro announcements.

**Why DoD awards.**
- **Exact timing.** DoD publishes every contract action of $7.5M or more each business day at
  about 17:00 ET.
- **Measurable size.** The dollar value can be compared with the winner's market cap.
- **Free and untested here.** The source is new to this project.

## Prior evidence

- Event studies of defense contract announcements find small positive abnormal returns on
  average. They are larger when the award is large relative to the firm, and for smaller
  contractors.
- Large primes (LMT, RTX, GD, NOC) receive awards almost daily, so a single award is rarely
  news for them. The hypothesis is about **material** awards.

## Decision

**Data.**
- **Awards:** `data/dod.sqlite` (`ingest-dod`), parsed from Internet Archive copies of
  defense.gov's daily "Contracts For <date>" articles. defense.gov itself blocks direct
  fetches.
  - Per paragraph: agency, awardee names, the first dollar amount, kind, and the small-business
    flag.
  - Only release dates 2015-01-01 → 2025-03-31 are stored.
- **Coverage:** articles missing from the archive are missing events. The output reports
  per-year coverage against the business days of each year. A year below 80% coverage is
  reported, and its events still count.
- **Prices:**
  - Track B (S&P 500/400) from `eventtrade.sqlite`, hedged with its sector SPDR (ADR 0011).
  - Other listed issuers from `smallcap.sqlite` (ADR 0012), hedged with IWM, with ADR 0012's
    tradability filter (raw price ≥ $5, median dollar volume ≥ $1M).
- **Shares outstanding:** SEC companyfacts `dei:EntityCommonStockSharesOutstanding`, the latest
  value filed before d (`ingest-sec-shares`, new, SEC User-Agent rules).
  - Market cap(d) = raw close(d−1) × shares.
  - An issuer-day without shares filed in the prior 400 days has no size and drops out.

**Awardee → issuer mapping (`core/dod_issuers.py`, frozen before any return is computed).**
- **Normalised exact match:** awardee names (case, punctuation, legal suffixes) against SEC
  `company_tickers` titles for issuers in either price store.
- **Manual subsidiary aliases** for the 300 awardee names with the most award dollars,
  2015–2025, regardless of match status. Examples: Sikorsky → LMT, Electric Boat → GD, Bath Iron
  Works → GD, Bell Textron → TXT, Raytheon Co. → RTX (UTX before 2020-04-03), Boeing units → BA,
  L3/Harris units → LHX (before the 2019-06-29 merger: L3 → LLL and Harris → HRS, if priced;
  otherwise dropped).
- **Rules:**
  - The mapping uses names and corporate history only (SEC filings and press releases), never
    prices.
  - An alias applies only between the ownership dates recorded with it.
  - A private or foreign parent maps to nothing.
- The file's SHA-256 is recorded in this ADR before computation.

**Events.**
- **Award size** of an issuer on release day d: the sum of `amount` over paragraphs where the
  issuer is the **sole** awardee.
  - Multiple-award paragraphs are excluded. Their amounts are shared ceilings, not revenue.
  - Paragraphs without an amount are excluded.
- **ratio** = size / market cap(d).
- **Cells:**
  - `material`: ratio ≥ 1%;
  - `moderate`: 0.1% ≤ ratio < 1%;
  - each × h ∈ {1, 5, 20} trading days, so **6 cells**. Sign **+**.
- **Timing:** the release comes after the close of d, so the event date is t = d and entry is
  the open of d+1. The outcome is the hedged open-to-open return over [d+1, d+1+h].
- **Declustering:** per issuer and cell, an event day is kept only if the issuer has no kept
  event in the same cell during the previous h trading days.

**Inference** (ADR 0011/0013 machinery, with the ADR 0014 permutation lesson).
- **Statistic:** calendar-time portfolio mean and NW t over horizon h.
- **Primary null:** matched placebo dates of the **same issuer in the same calendar year**.
  - Drawn from all of that issuer's tradable days in the year, **event days included**
    (a permutation).
  - 2,000 draws, seed `config.seed`, one-sided +.
- **Reported only:** the unmatched `eventstudy.placebo_p`; the Track B-only and small-only
  means; the mean by agency.
- **Discovery** 2016-01-04 → 2019-12-31: BH q = 0.10 across the 6 cells.
- **Confirmation** 2020-01-02 → 2025-03-31, for discovered cells. All are required:
  - Holm 0.05, one-sided;
  - a positive mean;
  - net of costs > 0, with a round trip of 2 × 10 bp (Track B) or ADR 0012's tiered small-cap
    cost, plus 2 × 1 bp for the hedge;
  - positive with 2020-02-20 → 2020-04-30 excluded;
  - positive for Track B issuers alone (the survivorship guard: the small-cap store holds only
    issuers still listed in 2026).
- **Logging:** each cell × period is a `signal` trial (track B, hypothesis `D`). Once only;
  logged after everything is computed.

## Consequences

- +6 discovery trials. Cost: Internet Archive fetches (~2,600 articles) and SEC companyfacts
  (a few hundred issuers). No BigQuery.
- **Survivorship:** the small-cap issuers are current listings, which biases a long-side effect
  upward. Hence the Track-B-only guard in confirmation.
- **Name mapping errors** add noise and bias toward zero. They are not a look-ahead, because the
  mapping is frozen before outcomes.

## Alternatives rejected

- *USAspending/FPDS action dates:* the DoD publication lag and the action-versus-announcement
  timing are uncertain (a look-ahead risk). The daily DoD release is the public event.
- *IDIQ multiple-award ceilings:* not revenue, so excluded.
- *A ratio grid:* two fixed buckets (0.1% and 1%), no search.

## Amendments after review (2026-10-11, before any award was matched to prices)

The scraper/parser and the share-count pull exist; no award has been mapped to an issuer or a
price. All review items are adopted; where they conflict with the Decision above, they win.

1. **Amount audit (blocker).** Size uses the **action amount**, never a cumulative or ceiling
   value.
   - **Audit procedure:**
     - Before any price is matched, the parser is audited by an agent on a stratified random
       sample of 200 sole-award paragraphs (100 with ratio ≥ 1%), seed `config.seed`.
     - For each paragraph the audit records: the action amount, the cumulative/ceiling amount,
       the obligated-at-award amount, the kind (new / modification / option / FMS), and whether
       it is a correction or de-obligation.
     - Corrections, de-obligations and negative amounts are excluded.
     - If the action-amount error rate exceeds 5%, the parser is fixed and re-audited on a fresh
       sample.
   - Every issuer-day with ratio > 50% is checked by hand (no prices) and listed.
   - **Reported only:** means by kind and by small-business flag, and size using the obligated
     amount.
   - The SHA-256 of the parser and of the audit file are recorded here.
2. **Prices for every mapped issuer (blocker).**
   - Daily prices are pulled for every mapped issuer CIK with a current listing, whatever its
     insider history, under ADR 0012's rules (raw ≥ $5, ≥ $1M median dollar volume, error
     screen).
   - **Before any price is read,** the output reports the count and award dollars of mapped
     events whose CIK has no current listing. They are classified merger versus failure as in
     ADR 0012 amendment 1.
   - Confirmation also requires ADR 0012's survivorship-stressed mean > 0.
3. **Mapping is mechanical, keyed by CIK and dated (blocker).**
   - **Aliases:**
     - normalised exact matches against SEC company titles and EDGAR former names (with
       dates);
     - subsidiary names found by EDGAR full-text search in **Exhibit 21** filings filed before
       d, mapped to the filer's CIK;
     - manual additions only with an SEC filing or company press-release URL. Financial news
       and price sites are never used.
   - **Not mapped:** joint ventures, and awardees matching several parents.
   - **Dated use:** an alias is used at d only if the parent CIK's price series at d belongs to
     that parent. For example, Raytheon Co. before 2020-04-03 has no priced CIK and is dropped.
   - **Blind audit:** a second agent, who never sees the first mapping, maps 150 random
     sole-award paragraphs. Disagreement is reported; above 10%, the rules are revised before
     the freeze.
   - The mapping file is frozen with a SHA-256 recorded here.
4. **Timing.**
   - d = the article title's date. Entry = the first regular-session open after 17:00 ET on d.
   - If d is not a trading day, entry is at the next open, and market cap uses the last close
     before d.
   - The earliest Wayback capture is used, and its lag to d is reported. Articles first captured
     more than 30 days after d are flagged and reported separately, but they count.
   - Information leaking before 17:00 biases the result toward zero (stated).
5. **Coverage** is reported against DoD release days (federal holidays excluded), by year and by
   month, together with September's share of events (fiscal-year end).
6. **Market cap.**
   - Shares = the sum over share classes of the cover-page share count from the latest filing
     dated before d, adjusted for splits between the cover date and d. The close is raw.
   - 20-F/40-F filers and ADRs drop out.
   - Issuers whose market cap is off by 100× or more from the 10-K cover float are listed and
     dropped.
7. **Net test:** event mean − placebo mean − round-trip cost > 0, as in ADR 0014 amendment 8.
   The 2 × 75 bp small-cap stress is reported. At h = 1, small-cap costs probably decide the
   outcome (stated in advance).
8. **Reported only:** means hedged with ITA instead of the sector/IWM hedge.
9. **Reading of the null, and a second pool.** The primary null tests "material award day versus
   the issuer's ordinary day". Reported only: a pool excluding every day within ±h of any
   sole award to the issuer.
10. **The Track-B guard is weaker than stated.** Track B is also current membership, so its
    sample is weaker on survivorship but not survivorship-free. Amendment 2's stress is the
    survivorship control.
11. **Power before outcomes.**
    - Before any return is read, the output reports events per cell × period, issuers per cell,
      and the largest issuer's share of events.
    - A minimum detectable effect is reported, with hedged σ fixed now: 2% for 1 day, 4.5% for
      5 days, 8% for 20 days.
    - A discovery cell with fewer than 40 events or fewer than 15 issuers is reported as
      **underpowered**, not null. It still enters BH with its p.
12. **Nits adopted:**
    - declustering runs from the first stored date (2015);
    - discovery events whose outcome window crosses 2019-12-31 are dropped;
    - the matched placebo draws with replacement and does not decluster, so it is approximately a
      permutation (pools are ~250 days per issuer-year, so ADR 0014's narrow-pool issue does not
      arise);
    - BH is valid under the cells' positive dependence;
    - the 1% / 0.1% thresholds are fixed a priori from the literature's materiality notion, not
      from this data.

## Implementation notes (2026-10-12, before any return was computed)

**Data.**
- `ingest-dod`: 2,548 articles with status ok, about 250 a year for 2015–2024, and 36,338 award
  paragraphs (865 corrections).
- defense.gov answers scripts with 403, so captures come from the Internet Archive.
- Two Aug/Sep 2025 articles (hold-out period, no prices) were opened while the parser was being
  built. They are not used.

**Mapping (amendment 3), frozen in `docs/dod/dod_issuer_map.csv`.**
- **Blind audit 1 (150 paragraphs):** 11.3% disagreement on the priced ticker, above 10%.
  Revisions:
  1. a former name stays valid until the CIK's last filing;
  2. titles also match without spaces;
  3. one-word titles own no prefixes;
  4. a title match to an unlisted CIK defers to a listed Exhibit 21 parent.
- **A first-word uniqueness rule** was tried in revision 2 and dropped. On sample 1 it created
  new false matches (e.g. Assured Information Security → Assured Guaranty).
- **Blind audit 2** (fresh 150): 14.7%. Almost all disagreements were misses; 1 was a wrong
  priced match (Sikorsky, three weeks after its sale, through the 400-day Exhibit 21 lookback).
- **Revision 4:** ' - ' and '/' also separate a head from a division, and titles ending in
  'Holding(s)' also match without it.
- **Blind audit 3** (fresh 150, evaluated once, then frozen): **8.7%**. Counting the BAE ADR rows
  as agreement, since ADRs are dropped, it is 6.0%. All are misses; there are 0 wrong priced
  matches.
- **Files:** `docs/dod/dod_audit/blind_sample*.csv`, `blind_map*.csv`, `compare_blind.py`.

**Amount audit (amendment 1).** Files: `docs/dod/dod_audit/amount_audit.csv` and
`ratio_over_50_check.csv`.
- **Parser error:** 0/200 on the action amount, and 1/47 among issuer-days above 50% of market
  cap (numbered actions in one paragraph; now summed).
- **But 40% of paragraphs are ceilings or multi-year totals** (IDIQ maximums, not-to-exceed,
  D-type contract numbers, funding set per order), including 25 of the 47 rows above 50%.
- **Decision** (amendment 1: size never uses a ceiling):
  - ceiling-flagged paragraphs (`dod_study.is_ceiling`) are excluded from size;
  - multiple-award language ('one of N contracts', 'compete for each order') is excluded;
  - their counts are reported.
- **Limitation:** merged multi-award paragraphs keep only the first awardee. The other awards are
  lost, which biases the result toward zero.

**Identity check (amendment 3).**
- Every (ticker, awardee) pair behind a candidate event (issuer-day size ≥ 0.1% of market cap)
  is checked against SEC business descriptions, Exhibit 21 lists and company press releases;
  no prices or news.
- Wrong pairs are listed in `docs/dod/dod_exclusions.csv` and dropped.

**Other implementation choices.**
- **ADRs and foreign ordinary shares** (5-letter OTC symbols ending in Y or F, e.g. BAESY)
  drop out (amendment 6).
- **Split history** is fetched up to today. Yahoo's split-adjusted closes are on today's share
  basis, so undoing them needs post-hold-out splits too. This is corporate-action bookkeeping,
  not returns.
- **Market cap** = adjusted close(d−1) × cover-page shares × product of the splits after the
  cover date.
- **Public-float check:** `dei:EntityPublicFloat`, with issuers at 100× or more dropped.
- **Survivorship:** f_fail = failure-classified unlisted issuer-days ÷ all mapped issuer-days in
  the period (ADR 0012 classification from EDGAR profiles).
- **Net:** event average − expected placebo average (the mean of the issuer-year pool means) −
  average round-trip cost.

**Freeze (commit `0afefa7`).** SHA-256 of the git-stored (LF) files:

| File | SHA-256 |
|---|---|
| `docs/dod/dod_issuer_map.csv` | `21d3e5f5062394ee01783268557987c83c5f4770b414ff62cb3f1bd9c25694f0` |
| `docs/dod/dod_exclusions.csv` | `c02e964e6b9b810797c517040df574825b60a681d63a3efd88d3815da1eee595` |
| `src/eventtrade/core/dod_parse.py` | `8431714f45a5f4ee98823eb728355d945822fc2e0f6cda8c8be30f92d40b76d5` |
| `src/eventtrade/core/dod_issuers.py` | `4ccfd8c87377af28d25aecc671712a88455002fd90d2c4ca1363a4c1329b2fce` |
| `src/eventtrade/core/dod_study.py` | `20418d59d5009bde4b98f2162e80cad5d0a327ec266df2ae80644761604d0533` |
| `docs/dod/dod_audit/amount_audit.csv` | `58b7e69e5720fe5609859f62eb3d48fcb496ca82759054347efbad4062e56c5e` |
| `docs/dod/dod_audit/id_check.csv` | `8b9878cc9822d833e8072b6d168bf0c45253b05357c327ae6ee48e540ba7bcf5` |

The identity check found 17 wrong pairs out of 345, plus 1 partial (Sikorsky under RTX from
2015-11-06). They are:
- 10 name collisions, the biggest being "SAIC" under Leidos (Leidos' pre-2013 name, now the
  new SAIC);
- 6 joint ventures the listed company does not control;
- 1 unit sold before the award.
