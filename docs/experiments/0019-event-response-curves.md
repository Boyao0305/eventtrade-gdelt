# 0019 — Event response curves (ADR 0017)

Date: 2026-10-16. Code: commit `3f0aa8e`, run once from the frozen checkout (`response-curves`),
after a blind dry run. 34 trials logged (hypothesis V, `lookahead = false`, so they count in N).

## Question

The user asked: does an event fail to show up because it moves prices on its own time scale and
then reverts, or because its direction varies? For each event type the study measures three
things against the same asset's ordinary days:
- **EV:** standardised excess variance, i.e. does it move prices at all;
- **S:** signed excess return, i.e. direction;
- **C:** continuation (+) or reversal (−) after the first day.

Horizons run 1–60 trading days from the last close before the event (15-minute steps for the
intraday SPY cells). A is 2016–2019, B is 2020–2025-03. CIs are at 99.4% (Bonferroni over 8
horizons, quarter blocks).

## Results

| Event type | Moves prices (EV > 0 in both periods) | Direction (S) | After day 1 (C) | Reading |
|---|---|---|---|---|
| Earnings (base close(d−2)) | yes, k = 2–40 | +0.3–0.4% at k = 3 in both periods | no drift, no reversal | directional (small premium); jump permanent |
| CPI/NFP, SPY daily | no | no | no | no response |
| CPI/NFP, SPY intraday | B only: variance ×1.4–1.75 from the open to 11:30 | no | no | no response (A shows nothing) |
| FOMC | A has 27 events (< 30) | — | — | insufficient. B intraday: variance ×2.1–2.8 from 14:15 to the close, no direction |
| DoD material / moderate | no | B only, material: +0.5–1.1% at k = 1–5 | no | no response |
| Insider purchases, all | A: k = 1–2 | +0.7–1.0% from k = 2 to 20 in A | no | directional (B is corrupted by a data error, see below) |
| Insider purchases, CEO/CFO | yes, k = 1–3 | +1.1–1.6% at k = 2–10 in both periods (A up to +3.2% at k = 60) | no | directional |
| GDELT conflict, DM / EM country ETFs | no | no (DM B: −0.2 to −0.5% at k = 20–60, B only) | no | no response |
| GDELT cooperation, DM / EM | no | no | no | no response |
| GDELT producer conflict, commodities (raw / SPY-hedged) | no | no | no | no response |
| GDELT company attention spike, negative tone | yes, k = 1–3, variance ×1.4–1.5 | no | no | **directionless (volatility)** |
| GDELT company attention spike, positive tone | yes at k = 1 (×1.3–1.5) | no | no | moves prices, direction unstable |

**Time scale.**
- **Earnings:** EV·k rises until about k = 5–10 and then stays flat to k = 40 (A: 2.4 → 11.1 →
  12.7 → 14.6 → 14.3). The earnings jump is **permanent**, with no measurable reversal; the
  period A half-life only appears at k = 60.
- **Attention spikes:** EV·k also stops rising after about 10 days (A, negative tone: 0.47,
  0.79, 1.06, 1.24, 1.48, 1.52 at k = 1, 2, 3, 5, 10, 20). Extra movement continues for about
  a week, then nothing reverts.
- **No event type shows a significant reversal (C < 0) in both periods.** The reversal
  half-lives in the table belong to single periods with non-significant C.

**Pre-event run-up** (CAR over the 5 days before the base): insider purchases −1.3% to −3.3%
(insiders buy after declines); earnings +0.1–0.2%; others about 0.

## Interpretation (the user's questions)

- **Country news (GDELT conflict, cooperation, producer conflict) does not move country or
  commodity ETFs at any horizon from 1 to 60 days, even in magnitude.**
  - It is not that the direction varied or that the wrong horizon was picked: there is no
    excess movement to find.
  - This confirms the earlier nulls. They were not caused by a time-scale mismatch.
- **Company news-volume spikes do move the stock, but in an unpredictable direction.**
  - Variance is 1.3–1.5× normal for 1–3 days, and the moves persist.
  - That is why every signed test (ADR 0005 E4/E4p, the intraday studies) found nothing.
  - These are **volatility events**: tradable only with options or volatility instruments,
    which needs option data (not stored, paid).
- **Reversal ("react, then revert") does not appear in any event type.** Where prices react
  (earnings, insiders, attention spikes, FOMC afternoons), the move stays.
- **Insider purchases** are the one non-earnings, non-macro event with a consistent direction:
  - +0.7–1.6% within 2–10 days of the filing, with CEO/CFO buys the strongest.
  - This is the effect found in experiments 0013/0014 (+2–3% over 60 days in the S&P 500/400,
    weaker in small caps). It did not pass costs, confirmation or survivorship there, and this
    diagnostic does not change those verdicts.
- **Earnings** show a small positive announcement-window premium (+0.3–0.4%) on top of the
  large permanent jump. It sits at about the 30 bp round-trip cost, so the earlier conclusion
  stands: the value of earnings is in predicting the direction of the jump (experiment 0018).
- **Macro:** CPI/NFP mornings move SPY more only in 2020–2025, and FOMC afternoons move it
  2–3× more (B only, n = 39), with no direction. That is also a volatility pattern.

## Limits

- **Data error.** V-INS-all period B is corrupted: CHRD on 2020-11-19 shows a one-day CAR of
  +257×. That is Yahoo's adjusted price across Oasis Petroleum's bankruptcy exchange, not a
  market move. EV is winsorized and unaffected, but S and C are not winsorized, so its signed
  B curve (+10–18%) and its "directional" B flags are unreliable. Without the 5 largest events,
  B's mean |CAR(1)| is 1.7%, which is normal. The insider reading rests on period A and the CEO/CFO
  cell (both clean). Future studies should winsorize signed CARs or screen price errors (ADR
  0012's `clean_opens`).
- FOMC period A is below the 30-event minimum (27 statements).
- Descriptive diagnostic: no costs, no trades.
- The hold-out is untouched.
