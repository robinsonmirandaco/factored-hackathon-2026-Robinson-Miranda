# ADR 4: An LLM with a prompt as the learned component, its ablation and the choice of model

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25; model choice on 2026-10-03
- Sources: `docs/reports/comprension_prueba.md`, `docs/reports/comprension_prueba_sonnet.md`, `docs/reports/analisis.md` (Ablations), `docs/reports/evaluacion.md` (Declarations), design section 6.1

## Context

The problem statement of the Factored AI & Data Hackathon 2026 says: "Evaluate at least one learned component against an appropriate baseline. Use valid labels or relevance judgments, prevent leakage, and justify representations, metrics, thresholds, and evaluation splits." This ADR is that component, its baseline and its evaluation; the labels are in ADR 6.

The task that needs it is understanding the customer's message: the dispute type and the clues that identify the charge (amount, date, merchant, channel, whether the customer has the card), written in free language, in four variants (ES-MX, ES-CO, ES-AR, PT-BR), with local money words, relative dates, voseo and portuñol.

## Decision

The learned component is **comprehension with an LLM and a prompt**: `claude-haiku-4-5-20251001`, prompt `comprehension-4`, temperature 0.

- It receives the message with personal data already redacted, and returns, validated by a Pydantic schema, the intent and each clue with the literal fragment of the message it came from.
- The code checks that every fragment is literally in the message; a clue without a faithful fragment is dropped.
- It never picks a tool, never provides a customer id and never decides autonomy (ADR 2).
- Deadline of 5 s, one retry, then the rules read the message (design 11.5).
- The prompt and its examples were tuned on the development split only; on the test split the harness checks that no example comes from outside development and that the prompt holds no id, base id or message of a calibration or test case (`155a220`). The held-out split was frozen with its hashes before the first tuning run.

## Alternatives considered: the ablation

Four systems on the same 376 held-out cases, by language and variant. Haiku: mean of 3 runs. Source: `docs/reports/analisis.md` (Comprehension: four systems on the same held-out cases).

| System | Intent F1 | Out-of-scope recall | In-scope sent out | Amount | Date | Merchant | Channel | Card possession | Faithful fragments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Rules (keywords, regular expressions) | 0.861 | 100.0% | 0.6% | 96.8% | 80.9% | 88.0% | 88.0% | 35.5% | 100.0% |
| TF-IDF + logistic regression (intent only) | 0.503 | 25.0% | 0.0% | n/a | n/a | n/a | n/a | n/a | n/a |
| Haiku 4.5 (3 runs) | 0.987 | 100.0% | 0.9% | 100.0% | 94.7% | 100.0% | 88.5% | 100.0% | 99.9% |
| Sonnet 5.5 (1 run) | 1.000 | 100.0% | 0.0% | 100.0% | 88.2% | 100.0% | 84.4% | 98.8% | 100.0% |

Where the language is hard, by variant (`docs/reports/comprension_prueba.md`, By variant):
- ES-MX intent F1: rules 0.792, Haiku 0.956; ES-MX channel: rules 64.6%, Haiku 83.3%.
- ES-AR date (voseo, relative dates): rules 62.5%, Haiku 88.9%.
- ES-CO merchant: rules 56.5%, Haiku 100.0%.
- Card possession, all variants: rules 35.5%, Haiku 100.0%.

Where the rules tie or win, reported as a finding: out-of-scope recall is 100.0% for both, and faithful fragments are 100.0% for the rules against 99.9% for Haiku. By intent, the rules reach 1.000 F1 on duplicates, as Haiku. Out of scope, Haiku's F1 is 0.941 against 0.960 for the rules, because it sends 0.9% of in-scope messages out (3 ES-MX cases, `docs/declarations.md` 6.4). Source: `docs/reports/comprension_prueba.md` (Intent F1 by intent).

TF-IDF + logistic regression falls from 0.850 intent F1 on calibration to 0.503 on the held-out split, below the rules. Development and calibration come from generator A and the test split from generator B and handwritten messages, so the probable cause is that the n-grams learned one generator's wording; these data cannot separate that from other causes. It is not a cheaper alternative here. Source: `docs/reports/analisis.md` (TF-IDF + logistic regression).

## Model choice (translated from the ADR of 2026-10-03, content unchanged)

### Context

- Story: TRZ-12 CA10 (model selection on the same held-out, on quality, cost and latency).
- Change from the design: `claude-sonnet-5-5` (the current Sonnet) was compared instead of `claude-sonnet-5`. Sonnet 5.5 does not accept `temperature`: it ran with default sampling and no reasoning (`thinking: between_tools`); Haiku runs at temperature 0.
- Sonnet ran with a 15 s deadline to measure its quality without fallbacks from the deadline; production uses 5 s.

### Rule, fixed before seeing results (2026-10-02)

Sonnet 5.5 replaces Haiku 4.5 only if, in its run on the held-out, it beats the mean of Haiku's 3 runs by at least 2 points in intent macro F1 or in amount accuracy, without worsening the rate of faithful fragments, with p95 latency under 5 s and a cost per case of at most twice Haiku's.

### Result on the held-out (376 cases)

| Criterion | Haiku 4.5 (3 runs) | Sonnet 5.5 (1 run) | Does Sonnet meet it? |
| --- | --- | --- | --- |
| Intent macro F1 | 0.987 | 1.000 | No: +1.3 points, less than 2 |
| Amount accuracy | 100.0% (124) | 100.0% (124) | No: 0 points |
| Faithful fragments | 99.9% (885) | 100.0% (848) | Yes, not worse |
| p95 latency | 2,427 ms (paid the first time, see below) | 2,705 ms | Yes |
| Cost per case | 0.00117 USD | 0.00285 USD (2.4 times) | No: more than twice |

Other metrics, with no weight in the rule: date 94.7% against 88.2%; channel 88.5% against 84.4%; card possession 100.0% against 98.8%; language 62.5% against 74.2%; in-scope cases sent out of scope 0.9% against 0.0%.

### Decision

Under the rule, Haiku 4.5 stays as the comprehension model.

### Notes

- Haiku's latency in its report on the test split shows 0 because its readings came from the harness cache (paid by TRAZO's runs). The latency paid the first time for those 1,128 readings is p50 1,465 ms and p95 2,427 ms (declared in `docs/reports/evaluacion.md`).
- Sonnet: 1 run instead of 3 for budget (option A of the plan, approved on 2026-10-02).
- Cost of the comparison: 1.0724 USD for Sonnet; Haiku with no new cost (harness cache).

## Consequences

- The LLM earns its place on card possession, merchants, relative dates and the ES-MX wording; the rules stay as the fallback when the LLM is slow, down or returns invalid output, and as the baseline every new prompt is measured against.
- Cost: 0.00117 USD per case for comprehension; the rules cost nothing.
- Variant detection is weak (62.5%), so the autonomy cell uses the language, not the variant (ADR 7).
- Any change to the prompt, its examples or the model goes through the same gate: held-out, rules baseline, 3 runs, versions recorded. The known improvements to comprehension wait for that gate (`docs/declarations.md` section 11).
