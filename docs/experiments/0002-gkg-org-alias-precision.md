# 0002: Matching S&P 500/400 names to GKG V2Organizations strings

Date: 2026-10-05. Measured on one day of development data (2024-03-12), before the hold-out.

## Question

Track B links each stock to GKG articles through `Asset.org_aliases`, which are exact lowercase
strings from `V2Organizations`. What do those strings look like, how many index members does
`core/aliases.make_aliases` find on a normal day, and how many of the matches are about the
company?

## Setup

- Reference: GKG 2.1 codebook (data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf),
  sections V1ORGANIZATIONS and V2ENHANCEDORGANIZATIONS. A V2 entry is `Name,charoffset`, and
  entries are separated by `;`. The same organisation appears once per mention. The extractor
  is "tuned to err on the side of inclusion", "certain smaller companies ... may be missed or
  occasionally misclassified as a person name", and the codebook suggests discarding names that
  appear only once or twice.
- Data: `gdelt-bq.gdeltv2.gkg_partitioned`, `_PARTITIONTIME = 2024-03-12`. That day has 455,442
  records, and 297,619 (65%) of them have a non-null `V2Organizations`. The partition holds
  English and machine-translated articles.
- Index lists: Wikipedia "List of S&P 500 companies" (503 rows) and "List of S&P 400 companies"
  (400 rows), fetched 2026-10-05 with the `Security` and `Symbol` columns. These are today's
  members applied to a 2024 day, so the survivorship caveat from ADR 0001 applies.
- Cost: 3 queries, each with a dry run first and `maximum_bytes_billed=3e9`. They billed 40.9 MB
  (org histogram), 79.7 MB (record counts plus URL diagnostic) and 89.1 MB (URL sample per
  alias). This was a raw client call from a scratch script because `adapters/gdelt_bq.py` did not
  exist yet. Nothing was written to `bq_usage`.
- Versions: Python 3.12, pandas 3.0.6, google-cloud-bigquery 3.46.1.

## Method

1. Histogram query: split `V2Organizations` on `;`, strip `,offset`, lowercase, and count
   mentions and distinct `GKGRECORDID` per string. This gave 211,036 distinct org strings.
2. Ran `make_aliases(Security, Symbol)` for every index member. A company counts as **covered**
   if at least one of its aliases has `is_ambiguous(alias) == False` and appears as an exact
   string that day.
3. Read every matched alias with its doc count, looking for false positives (below).
4. Sampled precision: for 25 high-volume aliases, pulled 12 article URLs each (deterministic
   pseudo-random by record id) and judged from the URL slug whether the article is about the
   company. This is a crude judgement, made by one reader from slugs only.
5. Changed the rules where the data contradicted them. The case table in
   `tests/core/alias_cases.py` was updated in the same change. The numbers below come from the
   final rules.

## Results

**What GKG org strings look like.** Raw values are Title Case ("Apple Inc", "Ibm"), so lowercase
them before matching. Dots and apostrophes never appear. Dotted initials are joined ("us bancorp",
"ch robinson worldwide inc", "jp morgan"). `&` is dropped and the gap collapses ("procter
gamble", "m t bank", "s p global inc", "jpmorgan chase co"). Hyphens are kept ("wal-mart"). Legal
suffixes are often kept ("goldman sachs group inc" 159 docs, "wells fargo company" 300, "apple
inc" 79). A possessive appears both as a stem and run together ("mcdonald" 505, "mcdonalds" 123,
"lowe companies"). Non-ASCII letters are sometimes dropped ("hans-bckler") and sometimes folded
("hans-bockler"). Translated articles produce word-order artefacts ("union european" 2,492 docs).

**Bare common-word brands are almost never extracted.** On this day the strings "apple",
"amazon", "tesla", "nike", "uber", "meta", "target", "gap", "ford", "3m", "bank of america",
"capital one", "moderna", "exelon" and "at t" have **zero** occurrences. Their suffixed forms do
occur: "apple inc" 79, "tesla inc" 37, "target corporation" 15, "gap inc" 6, "nike inc" 5,
"meta platforms inc" 59. The URL diagnostic confirms the gap: 9to5mac, Bloomberg and
tweaktown articles about Apple list other companies, or nothing, and list Apple only when the
text says "Apple Inc". The extractor's recall for dictionary-word company names is very low.
This is a property of the data, and no alias rule can recover it.

**Coverage** (company has at least one non-ambiguous alias present that day):

| alias family | S&P 500 (n=503) | S&P 400 (n=400) |
|---|---|---|
| lowercased Wikipedia name, as is | 37.4% | 15.8% |
| normalised core and suffix-strip stages | 45.7% | 19.5% |
| + strong suffix forms (inc, corp, corporation, co, company, companies, plc, ltd) | 62.2% | 30.0% |
| + initialism ticker, brand extras (final) | 62.2% | 30.0% |

On this day the initialism and brand-extra families add no covered companies. Their only matches
("adm", "google") are flagged ambiguous, or the company is already covered ("disney",
"exxonmobil", "ibm" is the Wikipedia name itself). Final non-ambiguous matched mass: 16,217
docs for the S&P 500 and 973 for the S&P 400. Across all 903 names, no alias is generated for two
different companies. Share classes of the same company deliberately share theirs.

**Sampled precision**, as the number of the 12 sampled articles that are about the company:

| alias | about the company | what the rest were |
|---|---|---|
| boeing, intel, chevron, alcoa | 9–10 | market roundups |
| adm (flagged) | 7 | "Adm." naval rank (Iran–Russia–China drill) |
| oracle (flagged) | ~6 | market roundups (Oracle reported earnings 2024-03-11) |
| united airlines, xcel energy | ~6 | sidebar/boilerplate pages (srilankasource.com) |
| walmart, apple inc, costco, kroger, mcdonalds | ~5 | lists of stores and retailers, generic market news |
| moody (flagged) | ~5, about the *rated* entity | persons named Moody |
| best buy | ~3 | product listings, "available at" |
| mcdonald (flagged) | ~3 | persons (Michael McDonald, Luke McDonald) |
| google (flagged), netflix (flagged), hyatt (flagged), visa (flagged), verizon | 1–2 | boilerplate ("Google News"), "streaming on Netflix", hotel venues, travel visas, a Beyoncé album launch (Verizon) |
| new york times (flagged), nasdaq (flagged), ryder (flagged), gartner (flagged) | 0 | cited as a source; "NASDAQ:XYZ" ticker tags; Pentagon spokesman Pat Ryder; analyst citations |

**False positives that changed the rules.**

- Invented "group"/"holdings" suffixes matched *other* companies: "cisco holdings", "brunswick
  group" (a PR firm), "reliance group" (Indian conglomerate), "domino group", "cooper group inc".
  The rule now appends only strong suffixes, plus a strong suffix after a stage that already
  ends in group/holdings ("unitedhealth group inc").
- A weak suffix on an ambiguous core is generic ("news group", "news co", "southern group").
  `is_ambiguous` now flags core + group/holdings/co when the core is ambiguous.
- Added to the ambiguous set from evidence: nasdaq, adm, new york times, gartner, moody, google,
  netflix, hyatt, mcdonald, ryder, crown corporation (a Canadian government entity type), and
  american international (the stripped core of AIG).

**False negatives that remain.**

- Dictionary-word brands, as above: AMZN, BAC, COF, MRNA, T, MMM and similar have no hit at all.
- Wikipedia short names drop words that GKG keeps: "Cisco" vs "cisco systems", "PayPal" vs
  "paypal holdings", "Honeywell Technologies" vs "honeywell international", "L3Harris" vs
  "harris corporation". "cisco systems" alone had 24 docs that day.
- Run-together brands: "ExxonMobil" is handled by a brand extra. "jp morgan" (56 docs) is not
  generated from "JPMorgan Chase".
- Prefixed marketing pages ("X company profile", "news ratings for X daily", "quarter for X") do
  name the company, but exact matching misses them.

## What this does not establish

- One day. Volumes are event-driven: Boeing's whistleblower news, ADM's restatement, Oracle
  earnings and the Xcel wildfire all fall on or near 2024-03-12. Coverage on a quiet day, or in
  2015 when GKG had fewer sources, may differ a lot.
- Precision is judged from URL slugs by one reader on 12 articles per alias. These are not
  labelled article texts, and the numbers are rough proportions without confidence intervals.
- "Covered" means the company appeared at least once that day. It says nothing about whether
  daily counts are dense enough for a z-score. Most S&P 400 names had 0–3 docs.
- The ambiguous list was tuned on this same day, so its precision is in-sample.
- Today's index members only. Delisted names are absent (survivorship, ADR 0001).
- Whether "about the company" or "mentions the company" is the better attention measure for B1/B2
  is a modelling question that this experiment does not answer.
