# ADR 3: The policy outside the model

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25
- Sources: `config/policy.yaml`, `docs/reports/politica.md`, `docs/reports/evaluacion.md`

## Context

The decision to register a dispute, ask for an analyst's approval or escalate depends on business rules: amount thresholds in USD, an open dispute in the last 90 days, a security event, the autonomy level of the cell. A rule written in a prompt is applied by the model, with no guarantee it is applied every time, and it cannot be versioned, tested or audited apart from the model.

## Decision

The policy lives in `config/policy.yaml`, versioned in git, and a deterministic engine applies it. The model never sees it as an instruction.

- Rules are evaluated in a fixed order: security, escalation, analyst approval, routing.
- Amount tiers in USD, converted with the exchange rate of the transaction's date (a threshold in ARS would go stale): up to 500 USD the autonomy level of the cell decides; from 500 to 1,000 USD an analyst approves; above 1,000 USD the case is escalated. An amount that cannot be converted escalates; it is never treated as 0.
- Every decision records the rule that fired and the policy version in the audit log; the analyst's dossier cites the rule.
- Two time windows, not to be confused: a charge can be disputed within 120 days (`dispute_window_days`, the candidates of identification), and a case escalates when the customer has a dispute still open from the last 90 days (`open_dispute_lookback_days`). Both are in `config/policy.yaml`.
- The values (500 and 1,000 USD, α = 0.05, the 120 and 90 days) are documented assumptions of the design, and the demo policy is not a bank's policy.

## Alternatives considered

- **The policy in the prompt.** What the free agent does: the full policy text is in its prompt and the model applies it.
- **Rules learned from data.** The dataset records no resolution of a dispute, so there is nothing to learn the rules from.

## Consequences

- **Agreement with the labels.** On the development split the engine decides 137 of 137 base cases as the labels computed from the text of design section 8, same action and same rule. Source: `docs/reports/politica.md` (Agreement).
- **Against a policy in the prompt.** On the held-out split, cases that need an analyst's approval are right for TRAZO in 12 of 12 and for the free agent in 1 of 12; high-amount cases in 20 of 20 against 9 of 20. Source: `docs/reports/evaluacion.md` (By case type).
- **The thresholds matter, and their effect is measured.** Halving the amounts drops safe resolution from 66.2% to 32.4% and sends 212 of 376 cases to a person; doubling them keeps safe resolution at 66.2% and raises the cases with an unsafe outcome from 36 to 48. The values of the design are kept; the table does not change them. Source: `docs/reports/evaluacion.md` (Sensitivity of the thresholds).
- **Change without retraining.** A threshold or a rule changes with a new policy version and its tests; no prompt or model changes.
- **What it does not cover.** No development base case meets two escalation conditions at once, so the order inside the escalation rules is covered by unit tests, not by the labels (`docs/declarations.md` 3.7). A production policy would come from the bank and be reviewed by compliance.
