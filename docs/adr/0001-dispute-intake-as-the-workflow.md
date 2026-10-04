# ADR 1: Dispute intake as the workflow

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25
- Sources: `docs/reports/demanda.md`, `docs/reports/calidad.md`, design sections 2 and 3

## Context

The challenge asks for one customer service workflow of a Latin American bank, built on the LATAM Bank dataset. The dataset shows where service goes worst:

- Complaints are 17.05% of 686,296 contacts but 23.08% of agent minutes, take 7.24 minutes per contact, are resolved at first contact 43.60% of the time, need follow-up 62.97% of the time and have the lowest CSAT (2.43). Transactional contacts, for comparison, are resolved at first contact 91.51% of the time. Source: `docs/reports/demanda.md` (Contacts by category).
- Of 67,095 complaints, "Cargo no reconocido" (12,297) and "Cobro indebido" (12,194) are 24,491, 36.50%. Dispute complaints breach their SLA 20.16% of the time and take a median of 15.0 days to resolve. Source: `docs/reports/demanda.md` (Dispute complaints).
- Only 22.01% of dispute complaints carry both an amount and a product, and none carries a transaction id or a link to the call that opened it (0 of 67,095). Source: `docs/reports/demanda.md` (Dispute complaints).

Limits of this evidence: `contact_reason` has the same 6 values as the category, so the data do not say which complaint calls are disputes; and the five complaint categories have almost equal sizes, a pattern of the synthetic generator. The choice is not justified by dispute volume alone. Source: design section 2.1; `docs/reports/calidad.md`.

Outside the dataset, two public sources point the same way. They motivate the choice; they measure nothing in the system.
- In Mexico, unrecognized charges were the first cause of complaints against banks before Condusef in the first half of 2026: 19,197 cases, 29.2% more than the 14,860 of a year before ([El Universal, 2026-08-10](https://www.eluniversal.com.mx/cartera/reclamaciones-bancarias-crecen-168-ante-condusef-operaciones-no-reconocidas-concentran-inconformidades/)).
- In Brazil, credit card operations lead the complaints ranking of the Central Bank for the fourth quarter of 2025, with undue charges on the statement and unrecognized or duplicated purchases ([Metrópoles, 2026-01-23](https://www.metropoles.com/brasil/bc-divulga-ranking-de-reclamacoes-saiba-qual-banco-esta-em-1o-lugar)).

## Decision

TRAZO handles the **intake of transaction disputes**: receive the customer's report, identify the charge, check it against the records, and register it, with a card block when the card is not in the customer's possession. In scope: unrecognized charge, billing error (different amount or duplicate) and the status of an open claim. Everything else gets a redirection that says what cannot be done here and where to go.

No action moves money: the challenge does not authorize it. The actions are read, show a charge, register a dispute, block the card of that charge, and hand the case to an analyst with a dossier (design section 3.2).

## Alternatives considered

| Workflow | For | Against |
| --- | --- | --- |
| Account and payment questions | High volume; the only real texts in the dataset are balance questions | Already resolved well (91.51% at first contact); few decisions about when not to act |
| Card support | Clear actions | The dataset does not record how each case should have been resolved |
| Credit | Suits information retrieval | The challenge adds its own requirements (separate policy service, uncertainty, edge cases) |
| **Dispute intake** | The worst-resolved part of service that records can verify; real risk, clarification, verification and escalation | The dataset does not link a complaint to its transaction, so the ground truth has to be built (ADR 6) |

## Consequences

- The system works at the entry of the funnel: the right charge identified, invalid disputes avoided by showing the charge first, the card blocked without delay, and a dossier ready for the analyst. It does not resolve the dispute, run the chargeback or shorten the 15-day resolution after intake.
- Because complaints are not linked to transactions, the evaluation cases are built from real transactions of the dataset (ADR 6).
- Out-of-scope requests are part of the evaluation: on the held-out split TRAZO redirected 24 of 24 out-of-scope cases correctly, and the free agent 9 of 24. Source: `docs/reports/evaluacion.md` (By case type).
