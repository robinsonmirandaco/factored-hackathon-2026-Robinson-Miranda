# Model card: the LLM components of TRAZO

TRAZO uses one LLM, `claude-haiku-4-5-20251001`, in two roles: **comprehension**, the learned component evaluated against baselines (ADR 4), and **composition**, the wording of some replies within verified facts. Neither role picks a tool, provides a customer id, applies the policy or decides autonomy; those are code (ADR 2, ADR 3, ADR 7).

## 1. Model and versions

| | Comprehension | Composition |
| --- | --- | --- |
| Model | `claude-haiku-4-5-20251001` | `claude-haiku-4-5-20251001` |
| Prompt | `comprehension-4` (`config/prompts/comprehension.yaml`) | `COMPOSE_SYSTEM` in `src/app/adapters/llm.py`, recorded as `sha256:b145fa9b1ed8` |
| Temperature | 0 | Not set: the provider's default sampling |
| Deadline and retry | 5 s per attempt, one retry, then the rules read the message | Same; on failure a fixed template is sent |
| Output | JSON validated by a Pydantic schema | Free text, checked by the fact checker before it is sent |

Every evaluation run records the model, the prompt versions, the policy version, the seeds and the split hashes (`eval/runs.jsonl`). At startup the API makes one comprehension call with a fixed synthetic message in the background (15 s, no retry), so the first request after a deploy does not pay a cold call; it does not block `/health` and is turned off with `LLM_WARM_UP=false`.

## 2. Intended use

**Comprehension.** Read one customer message about a card or account charge, in Spanish (Mexico, Colombia, Argentina) or Portuguese (Brazil), and return:
- the intent: unrecognized charge, billing error by amount, billing error by duplicate, status of a claim, or out of scope;
- the clues that identify the charge (amount and currency, approximate or not; date expression; merchant; channel; whether the customer has the card), each with the literal fragment of the message it came from;
- the language variant.

The code checks each fragment against the message and drops a clue whose fragment is not literal. What comprehension returns feeds identification (ADR 5) and the policy (ADR 3); it never triggers an action by itself.

**Composition.** Write in the customer's language the informative turns, the presentation of options, the closing of a recognized charge and the explanation of a pending duplicate, using only the facts of the turn. Handoffs to a person, the confirmation question, the registration receipt and every security, failure and redirection reply are written by code (`docs/declarations.md` 5.4).

**Inputs.** The message with personal data already redacted: the customer's name, card numbers of 13 to 19 digits with a valid Luhn digit, CPF, CNPJ and RUT, a CURP by its shape, a cédula or DNI after its keyword, account numbers after their keyword, emails, phones with a country code or a national grouping, and any bare run of 10 digits are replaced by placeholders before the call (`src/app/domain/pii.py`). The model never receives a customer id.

## 3. Out of scope

- Deciding whether to register, block, ask, escalate or approve.
- Choosing a tool or reading data of its own.
- Any message outside card and account charges: comprehension classifies it as out of scope and the code answers with a fixed redirection.
- Languages other than Spanish and Portuguese; the variants are only those four.
- Real customers: it was built and evaluated on synthetic data with generated messages.

## 4. Evaluation

On the held-out test split: 376 cases from 94 base cases, each in four variants [offline]. Haiku ran 3 times; mean and range. Source: `docs/reports/comprension_prueba.md`; `docs/reports/analisis.md` (Ablations).

### Overall, against the baselines

| System | Intent F1 | Out-of-scope recall | In-scope sent out | Amount | Date | Merchant | Channel | Card possession | Faithful fragments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Rules | 0.861 | 100.0% | 0.6% | 96.8% | 80.9% | 88.0% | 88.0% | 35.5% | 100.0% |
| TF-IDF + logistic regression (intent only) | 0.503 | 25.0% | 0.0% | n/a | n/a | n/a | n/a | n/a | n/a |
| **Haiku 4.5 (3 runs)** | **0.987** | 100.0% | 0.9% | 100.0% | 94.7% [94.4%, 94.8%] | 100.0% | 88.5% [88.0%, 89.1%] | 100.0% | 99.9% |
| Sonnet 5.5 (1 run) | 1.000 | 100.0% | 0.0% | 100.0% | 88.2% | 100.0% | 84.4% | 98.8% | 100.0% |

Field accuracy is over the cases whose first message carries the clue (amount 124, date 288, merchant 92, channel 192, card possession 172); merchants named only by their category are not scored (28 cases). Source: `docs/reports/comprension_prueba.md` (How each metric is scored, Overall).

### By language and variant (Haiku against the rules)

| Group | Intent F1 | Date | Merchant | Channel | Card possession | In-scope sent out |
| --- | --- | --- | --- | --- | --- | --- |
| es | 0.983 (0.867) | 93.4% (79.2%) | 100.0% (84.1%) | 90.3% (85.4%) | 100.0% (36.4%) | 1.1% (0.0%) |
| pt | 1.000 (0.847) | 98.6% (86.1%) | 100.0% (100.0%) | 83.3% (95.8%) | 100.0% (32.6%) | 0.0% (2.3%) |
| es-MX | 0.956 (0.792) | 92.6% (76.4%) | 100.0% (100.0%) | 83.3% (64.6%) | 100.0% (44.2%) | 3.4% (0.0%) |
| es-CO | 1.000 (0.907) | 98.6% (98.6%) | 100.0% (56.5%) | 93.8% (95.8%) | 100.0% (27.9%) | 0.0% (0.0%) |
| es-AR | 1.000 (0.878) | 88.9% (62.5%) | 100.0% (95.7%) | 93.8% (95.8%) | 100.0% (37.2%) | 0.0% (0.0%) |
| pt-BR | 1.000 (0.847) | 98.6% (86.1%) | 100.0% (100.0%) | 83.3% (95.8%) | 100.0% (32.6%) | 0.0% (2.3%) |

Rules in parentheses. Amount accuracy is 100.0% for Haiku and 96.8% for the rules in every group. Source: `docs/reports/comprension_prueba.md` (By language, By variant).

Where the rules tie or do better, a finding: out-of-scope recall (100.0% both), faithful fragments (100.0% against 99.9%), channel in Portuguese (95.8% against 83.3%) and in ES-CO and ES-AR, and the share of in-scope messages sent out in ES-MX (0.0% against 3.4%).

### Variability over 3 runs

Intent F1 0.987 in every run; date 94.4% to 94.8%; channel 88.0% to 89.1%; first-turn intent identical in 376 of 376 cases across the 3 runs of the full system. Source: `docs/reports/comprension_prueba.md`; `docs/reports/analisis.md` (Variability over repetitions).

### Cost and latency

- Comprehension: 0.00117 USD per case (list prices on 2026-10-02, prompt cache included).
- Latency first paid for the 1,128 held-out readings: p50 1,465 ms, p95 2,427 ms. Haiku's report shows 0 because those readings came from the harness cache (`docs/declarations.md` 6.2).
- Sonnet 5.5 for comparison: 0.00285 USD per case, p50 2,012 ms, p95 2,705 ms (15 s deadline). Source: `docs/reports/comprension_prueba_sonnet.md`.

### Effect on the full system

With Haiku, TRAZO reaches 66.2% safe automated resolution against 28.7% for the free agent on the same model (ADR 2). Comprehension caused 3 of TRAZO's 36 unsafe outcomes on the held-out split (in-scope messages read as out of scope, all ES-MX) and contributed to 3 more (card possession read differently from the label). Source: `docs/reports/analisis.md` (Failures by stage and cause).

No difference in safe resolution or unsafe outcomes by variant, language, segment, country or provenance crossed the rule fixed before computing (95% bootstrap interval of the difference excludes 0): 28 comparisons tested, 0 crossed, 4 not tested for size. Source: `docs/reports/analisis.md` (Disparities).

## 5. Leakage controls

- The prompt and its examples were tuned on the development split only.
- The held-out split was frozen with its hashes before the first tuning run.
- On the test split, `check_prompt_sources` verifies that every example cites a development case and that the prompt holds no id, base id or message of a calibration or test case (`155a220`; `pipeline/comprehension_llm.py`). The test report says "prompt examples checked against the development, calibration and test splits (none cited or contained)" (`docs/reports/comprension_prueba.md`, Run).

## 6. Choice of model

Fixed before seeing results: Sonnet would replace Haiku only with at least 2 points more of intent F1 or amount accuracy, no worse faithful fragments, p95 under 5 s and at most twice the cost per case. Sonnet gained 1.3 points of intent F1 and 0 of amount at 2.4 times the cost, so Haiku stays. Sonnet's p95 (2,705 ms) is under the production deadline of 5 s; the report does not give the exact share of its calls over 5 s. Source: ADR 4.

## 7. Limitations and risks

- **Variant detection is weak:** 62.5% of messages get the right variant; the autonomy cell uses the language. Source: `docs/reports/comprension_prueba.md`.
- **In-scope messages read as out of scope:** 3 ES-MX cases were redirected while their other variants were registered (`docs/declarations.md` 6.4). Short answers to a pending question ("fueron 900") can also be read as out of scope; the code compensates by reading an answer with a clue as an answer, and a prompt that gets the pending question waits for the gate (`docs/declarations.md` section 11).
- **Channel in Portuguese:** 83.3% against 95.8% for the rules; part of the channel loss comes from ambiguous labels (`docs/declarations.md` 3.6).
- **Composition can promise what the system does not do:** the closing of a recognized charge and the explanation of a pending duplicate are written by the LLM, and the fact checker does not catch every new wording of a contact promise (`docs/declarations.md` 10.3). With the checker off, 12 such claims reached customers in 9 of 376 cases (ADR 8).
- **Cosmetic wording:** amounts formatted differently from the card on screen, and "Hola" on every turn (`docs/declarations.md` 10.7).
- **Prompt injection:** at the commit of the single run there was no injection detector, and 15 injection cases failed as `should_have_escalated`, with 0 successful injections (the code never let the LLM act). A detector that reads the message before comprehension was added after the run and measured on development only (`docs/declarations.md` 2.1, 3.1).
- **Synthetic, generated evaluation:** all Portuguese and all variants are generated; real customers may write differently, and coverage of identification depends on how close real messages are to these (ADR 5, ADR 6).
- **Provider dependence:** the model is an external API. A slow, failed or rejected call falls back to the rules within 5 s and one retry; the deployed demo's key expires on 2026-10-26, after which the demo runs on the rules (`docs/declarations.md` 9.8).
- **Privacy:** redaction misses an address written in the message, and a 10-digit amount written with no separator would be redacted as a phone (`docs/declarations.md` 10.1).
