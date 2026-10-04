# ADR 6: Ground truth built from real transactions

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25
- Sources: `eval/splits/manifest.json`, `docs/reports/demanda.md`, `docs/reports/evaluacion.md` (Cases), design section 6.3

## Context

To measure whether the system registers the right charge, each evaluation case needs its true transaction and its correct action. The dataset cannot give them: complaints carry no transaction id and 0 of 67,095 are linked to the call that opened them (`docs/reports/demanda.md`), and the texts of the dataset are templates. There is no Portuguese text and no Brazilian customer.

## Decision

Every evaluation case is born from a real transaction of the dataset:

1. A real transaction of a real customer of the cohort is drawn, stratified by country, segment, channel and type.
2. A noise model writes what a customer would say: exact, rounded or approximate amount; exact or relative date; full, partial, misspelled or category-only merchant; channel mentioned or not. The chance that a clue is missing follows the dispute complaints: amount present in 33.18%, product in 66.38% (`eval/splits/manifest.json`, `presence_rates`).
3. A generator writes the message in the case's language and variant; the source transaction, the true value of each clue and the expected action under the policy of design section 8 are known, so **the labels are exact by construction**.

| Split | Built by | Base cases | Cases | Use |
| --- | --- | --- | --- | --- |
| Development | Generator A | 150 | 600 | Tune the prompt, weights and temperature |
| Calibration | Generator A, other customers | 100 | 400 | Compute q-hat |
| Test (held-out) | Generator B (another prompt and templates) | 79 | 316 | Final results |
| Test (held-out) | Handwritten set, reviewed one by one | 15 | 60 | Final results |

Each base case runs in ES-MX, ES-CO, ES-AR and PT-BR. No customer is in two splits and test transactions are later than development and calibration ones, checked when the splits are built (`pipeline/cases/splits.py`). Each split file has its sha256 in the manifest, checked whenever it is loaded. Source: `eval/splits/manifest.json`.

## Alternatives considered

- **The dataset's complaint texts.** Templates, with no link to a transaction: no true charge to score against.
- **An LLM as judge** of the system's answers. Not used: the ground truth is deterministic and each run is scored on the final state of the database and the audit log, not on the text (`docs/reports/evaluacion.md`, Cases).
- **Hand-labeling every case.** Not feasible at 376 held-out cases in the time available; a handwritten set of 60, reviewed one by one, was added instead, and its labels reviewed twice.

## Consequences

- Generated labels are exact; the handwritten ones were reviewed twice from a blank sheet, agreement 1.000 on the eight fields and kappa 1.000 on intent and action; against the constructed labels, action agrees on 48 of 60 (`docs/reports/evaluacion.md`, Cases; provenance in `docs/declarations.md` 3.3).
- Generator B differs from generator A as intended: TF-IDF trained on generator A falls from 0.850 to 0.503 intent F1 on the test split (ADR 4).
- Limits: all Portuguese, all regional variants and the misunderstanding scenarios are generated, declared as such; the noise model is ours, not real customers' (`docs/declarations.md` 7.2, 10.6). Some labels are debatable: the injection labels (`docs/declarations.md` 3.1, 3.2) and ambiguous channel labels (3.6).
- The held-out files live outside the repository with the data; every access to them is recorded (`docs/declarations.md` 1.2, 1.3).
