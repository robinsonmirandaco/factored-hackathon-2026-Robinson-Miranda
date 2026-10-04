# ADR 5: Identification with conformal prediction

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25
- Sources: `docs/reports/identificacion.md`, `docs/reports/densidad.md`, `docs/reports/analisis.md` (Ranker against the manual score), design section 6.2

## Context

Before registering a dispute the system has to know which transaction the customer means, and customers describe charges loosely: "about 1,800 pesos last week at MercaYa". On the simulated date (2026-06-17), 30.2% of customers have 2 or 3 disputable transactions in the 120-day window and 19.0% have more than 3: about half of all customers have more than one candidate. Source: `docs/reports/densidad.md` (2026-06-17). Acting on the wrong charge is an unsafe outcome; asking every time defeats automation. The question is when the system knows enough to act.

## Decision

- **Score.** An additive score per candidate with five interpretable components: amount (log distance with a tolerance by how the customer said it), date (distance to the understood window, soft by decision), merchant, channel and currency. Weights fitted on development (Haiku readings: amount 0.2, date 0.1, merchant 0.5, channel 0.2, currency 0.0); softmax with a temperature fitted on development (0.0430). Source: `docs/reports/identificacion.md` (Fitted parameters).
- **Split conformal.** On the calibration split, the score of each case is 1 minus the probability of the true transaction; q-hat is its quantile at α = 0.05 (q-hat 0.8000). The base case is the unit: its four variants share a transaction and are not independent, so the largest score of the four is used.
- **Decision by set size.** 1: identified, show it for recognition; 2 to 3: show them as options; more than 3: ask for one detail, then escalate; empty by the absolute rejection threshold or with no candidate: not found, escalate; empty with no rejection and more than one candidate: ask for a detail (decided on 2026-09-30, `8cbf75f`, before the run).
- **Two doors.** A dispute that starts from the "No lo reconozco" button arrives with the charge identified; it is reported apart so it does not inflate identification.

## Alternatives considered

- **Top-1 with no set.** Acts on the most probable candidate; its accuracy on the held-out split is 82.6% with Haiku, so about 1 in 6 would be acted on the wrong charge.
- **A fixed threshold on the probability.** Gives no coverage guarantee and needs its own tuning.
- **A trained ranker** (logistic regression on the same five components), evaluated in the ablation: at the same α both scores reach the same coverage within 2.0% and mean set sizes within 0.05, and the ranker has a higher development NLL (0.300 against 0.289). It buys nothing over the manual score and its five readable weights, which stays. Source: `docs/reports/analisis.md` (Ranker against the manual score).

## Consequences

On the held-out split, 304 cases of the identification population from 76 base cases [offline]. Source: `docs/reports/identificacion.md` (Test split).

| Comprehension | Coverage | Coverage by base case | Mean set size | Size 1 | Brier | ECE | Top-1 accuracy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Rules | 95.7% (291/304) | 93.4% (71/76) | 1.45 | 66.1% | 0.2311 | 0.0538 | 78.9% |
| Haiku (3 runs) | 97.9% (298/304) | 95.6% (73/76) | 1.43 | 66.3% | 0.1963 | 0.0695 | 82.6% |

- By language with Haiku: Spanish 97.7%, Portuguese 98.7%; by provenance: generator B 97.5%, handwritten 100.0%.
- In end-to-end runs no dispute was registered on the wrong charge: 0 of 376 for TRAZO, 7 of 376 for the free agent (`docs/reports/evaluacion.md`, Unsafe outcomes).
- The guarantee is marginal and holds under exchangeability with cases calibrated on generated text; groups can fall below 95%: missing_data 16/20 (80.0%; 58.4% to 91.9%) on the test split. Real traffic would need recalibration and coverage monitoring.
- The amount tolerance was derived from the noise model after a test case revealed the inconsistency; the coverage on the test split may be slightly biased in favor of the system (`docs/declarations.md` 7.2).
- Lowering α costs little: at α = 0.10 safe resolution is 65.1% against 66.2% (`docs/reports/evaluacion.md`, Sensitivity of the thresholds).
