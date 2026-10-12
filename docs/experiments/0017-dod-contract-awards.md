# 0017 — DoD contract awards (ADR 0015)

Date: 2026-10-12. Code: commit `6e58316`, run once from the frozen checkout (`dod-study`), with the
issuer map, exclusions, parser and study core frozen by SHA-256 in the ADR. 6 trials logged
(hypothesis D, discovery only). The store holds 8,202 trials.

## Question

Does a DoD contract award that is large relative to the winner's market cap move the winner's
stock after the 17:00 ET release? Returns are hedged with the sector SPDR (Track B) or IWM
(other listed issuers), open to open from d+1.

## Data

**Awards and coverage.**
- 36,338 award paragraphs from Internet Archive captures of defense.gov's daily releases.
- Coverage is 246–251 articles a year out of 249–252 federal business days.
- First captures have a median lag of 3 days, a 90th percentile of 177 days, and 741 articles
  were first captured more than 30 days late. Their events count and are reported separately.

**Mapping** (frozen after three blind audits; final disagreement 8.7%, all misses, 0 wrong
priced matches) and the amount audit (0% parser errors, but 40% ceilings):
- 12,767 mapped sole-award paragraphs.
- 4,572 ceilings were excluded from size.
- 18 wrong identity pairs were excluded.
- That leaves 8,195 awards and 6,669 issuer-days across 214 issuers.

**Survivorship.**
- Mapped issuer-days of issuers with no current listing: 538 of 2,784 in discovery, 113 of
  3,202 in confirmation.
- They were mostly mergers. f_fail is 0.026 and 0.004.

**Market caps.**
- 776 listed issuer-days have no share count, and 100 are not tradable.
- The public-float check dropped 7 issuers: KTOS, WWD, OSIS, DUK, MLKN, ECHO, SPIR (see Limits).
- ADRs dropped: BAESY, BLFBY.

**Power (before outcomes).**
- Discovery events: 163 / 154 / 130 material (33 issuers) and 542 / 451 / 299 moderate (38
  issuers) at h = 1 / 5 / 20. No cell is underpowered.
- Minimum detectable effects for the material cells: 0.39% (h1), 0.90% (h5), 1.74% (h20).

## Results (discovery 2016-01-04 → 2019-12-31)

Event and placebo columns are event averages. The placebo is the same issuer's ordinary days in
the same year.

| Cell | n | Event mean | Placebo | NW t | Matched p | Net of costs | Track B / small |
|---|---|---|---|---|---|---|---|
| material h1 | 163 | +0.18% | +0.03% | 1.44 | 0.120 | −0.29% | +0.31% / −0.05% |
| material h5 | 154 | +0.37% | +0.15% | 1.25 | 0.202 | −0.23% | +0.49% / +0.14% |
| material h20 | 130 | +1.49% | +0.68% | 2.06 | 0.121 | +0.38% | +1.19% / +2.09% |
| moderate h1 | 542 | −0.09% | +0.03% | −1.96 | 0.994 | −0.35% | |
| moderate h5 | 451 | −0.12% | +0.13% | −0.52 | 0.937 | −0.49% | |
| moderate h20 | 299 | +0.08% | +0.57% | −0.05 | 0.941 | −0.74% | |

**BH (q = 0.10): 0 of 6 cells discovered.** The confirmation period was not evaluated.
**Verdict: null.**

**Reported only, material h20:**
- ITA-hedged mean: +1.37%.
- Placebo mean when the pool excludes days near any award: −0.41%.
- Unmatched placebo p: 0.086.
- 70 of the 130 events come from late-captured articles.

## Interpretation

- **Material awards point the expected way at every horizon**: +0.18% the next day and +1.5%
  over 20 days, against roughly half that on the issuer's ordinary days. The evidence is weak
  (p 0.12) and does not pass BH across 6 cells.
- **The material h20 cell** is the only one with a positive net (+0.38%). It is concentrated in a
  few issuers: HII, V2X/Vectrus and Fluor make up a large share.
- **Moderate awards (0.1–1%) carry no information**, consistent with the market already
  expecting routine awards to primes.
- Taken at face value, a 1-day reaction of about 0.15% is far below the 0.45% average round
  trip, so the 1-day trade is not viable even if real.

## Limits

- **Float-check false drops.** The check dropped 7 issuers at ratios around 1,000×. That pattern
  fits public floats tagged in thousands in XBRL, not bad share counts. The rule was registered
  and has run, so it stands, but it removed relevant defense names (Kratos, Woodward, OSI).
  Future studies should check units before applying the float check.
- **Lost awards:** merged multi-award paragraphs keep only the first awardee, and mapping misses
  run at about 25% recall loss. Both bias toward zero.
- **Excluded ceilings:** ceiling awards (40%) are excluded from size, so the large IDIQ wins
  investors may react to are not tested.
- **Survivorship:** the small-cap issuers are current listings.
- The hold-out is untouched.
