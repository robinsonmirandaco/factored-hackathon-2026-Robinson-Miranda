# ADR 2: A governed workflow over a free agent

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25
- Sources: `docs/reports/evaluacion.md`, `docs/reports/analisis.md`, design sections 4 and 13.1

## Context

An LLM agent with tools can run the whole intake: read the message, pick tools, decide whether to register, and write the reply. In a bank, three things go wrong with that: the model applies the policy from a prompt and can skip a rule; it can state an amount, a date or an action that no record backs; and its autonomy is fixed, with no evidence of when it is safe. The question for this decision is who decides each step.

## Decision

TRAZO is a workflow, not an agent: the path is a state machine in code, and the LLM has two roles inside it, understanding the message (structured extraction with literal fragments) and writing the reply within verified facts.

| Step | Who |
| --- | --- |
| Next step and tool | The state machine |
| Whether to act | The policy in YAML (ADR 3), the conformal set (ADR 5) and the autonomy level of the cell (ADR 7) |
| What the LLM does | Understand and write |
| How an action is checked | The code reads the database back after writing, and a deterministic fact checker reads the reply (ADR 8) |
| The customer id | Only from the session token; the LLM never provides one |

The agent framework is our own state machine. LangChain, LangGraph and similar frameworks were left out: a framework adds layers that do not help evaluate the system (design section 11.2).

## Alternatives considered

- **Free agent (the baseline, measured).** One LLM with a long prompt and every tool by tool calling, with no conformal identification, no verifier and no autonomy watch. For a fair comparison it has the same model, the same tools with the same access control, the same simulated client, the full policy text in its prompt, and two steps taken from TRAZO's code (how a charge is searched and showing it before disputing). Its prompt had one iteration on the development split, as TRAZO was tuned there (`docs/declarations.md`, section 4).
- **An agent framework** with sub-agents per intent: rejected for the reason above.

## Consequences

Measured on the held-out split, 376 cases from 94 base cases [offline], TRAZO with 3 runs and the free agent with 1. TRAZO's 3 repetitions gave identical results on the five measures, the unsafe outcomes and the dossiers; only latency, turns and cost varied, and 1 case of 376 ended in an approval in two repetitions and a handoff in the third, with no change in any measure (`docs/reports/evaluacion.md`, Repetitions; `docs/reports/analisis.md`, Variability over repetitions). Source: `docs/reports/evaluacion.md` (The five measures, Unsafe outcomes, Operational efficiency); `docs/reports/analisis.md` (Invariance).

| Measure | TRAZO | Free agent |
| --- | --- | --- |
| Safe automated resolution | 66.2% [56.8%, 75.6%] (233/352) | 28.7% [22.2%, 36.1%] (101/352) |
| Missed escalations | 26.0% [10.6%, 44.2%] (27/104) | 71.2% [58.7%, 82.7%] (74/104) |
| Unnecessary escalations | 0.0% (0/260) | 5.8% [3.1%, 9.2%] (15/260) |
| Dossier completeness | 77/77 complete | 0/45 complete |
| Cases with any unsafe outcome | 36/376 | 232/376 |
| Claims without a source sent | 0/376 | 212/376 |
| Disputes on the wrong charge | 0/376 | 7/376 |
| Decision changes between language variants | 5.3% of base cases (repetition 1) | 53.2% |
| Latency per case, p50 / p95 | 1,844 / 4,242 ms | 10,888 / 16,507 ms |
| Cost per safe resolution | 0.00233 USD | 0.03234 USD |

The paired difference in safe automated resolution is +37.5 points [+30.1, +45.7].

What it costs:
- TRAZO attempts less automation (78.1% against 88.1% of in-scope cases) and contains less (79.5% against 88.0% of all cases): it hands more cases to a person. Containment is reported apart from resolution because a contained case can still be wrong.
- Every rule is code, so a new behavior needs a code change, a test and a new policy version; the free agent would only need a prompt edit.
- TRAZO's 36 unsafe outcomes have four causes (`docs/reports/analisis.md`, Failures by stage and cause): an injection in the text not flagged (15) and another customer's id in the text not flagged (12), both addressed after the run (`df22469`, `b66cfa1`); the card blocked after the registration failed (6), addressed after the run (`2ef62a0`); and three ES-MX messages read as out of scope and redirected (3), declared and not fixed. The changes after the run were measured on development only (`docs/declarations.md`, section 2).
