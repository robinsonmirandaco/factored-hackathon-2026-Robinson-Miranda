# ADR 8: A deterministic fact checker over a second LLM

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25; the second LLM validator of the starting scaffold was removed in TRZ-20
- Sources: `docs/reports/evaluacion.md` (Unsafe outcomes), `docs/reports/analisis.md` (Fact checker on and off), `src/app/domain/fact_check.py`, design section 6.5

## Context

A reply that states an amount, a date, a folio, a deadline or an action that no record backs is an unsafe outcome. A common pattern checks the reply with a second LLM call. That adds tokens and latency, and its verdict is another model's opinion, not evidence.

## Decision

Each reply the LLM writes is built from a dictionary of verified facts of the turn (records read, actions read back, policy passages). Before sending, a deterministic checker extracts amounts, numbers, dates, folios, deadlines, card digits, merchants, action claims, contact promises, vague deadlines and forbidden requests from the text and checks each against the facts. If anything is not backed, the reply is dropped and a fixed template is sent; the block is recorded. A policy claim with no passage behind it is never made. Replies that must be exact (handoffs, the confirmation question, the receipt, security and failure replies) are written by code and never reach the LLM (`docs/declarations.md` 5.4).

## Alternatives considered

- **A second LLM as validator** (the starting scaffold had one): removed, for the cost and latency above and because its verdict cannot be audited.
- **No checker**, trusting the prompt: measured in the ablation below.

## Consequences

- **Ablation on the held-out split**, same commit and same cache, checker off [offline]: 12 claims without a source sent in 9 of 376 cases (contact promise 5, vague deadline 4, number 3), against 0 with the checker on; safe resolution 63.6% against 66.2%; cases with any unsafe outcome 45 against 36. Source: `docs/reports/analisis.md` (Fact checker on and off).
- **The free agent, in observer mode:** 212 of 376 cases with a claim without a source, led by numbers (194), amounts (149), contact promises (59) and dates (48); TRAZO 0. Source: `docs/reports/evaluacion.md` (Unsafe outcomes).
- Cost: no tokens and no network call.
- Multipliers: since `4c855a3` "mil", "lucas" and "millones" are read as part of an amount, both ways: with 57000 among the facts "57 mil" passes, and with only 57 it is caught. No reply TRAZO sent in the held-out runs has such a figure, so its measures do not change; the free agent's observer counts could (`docs/declarations.md` 4.5).
- Limits, declared: it does not read numbers in words, does not catch an invented merchant missing from the customer's list, and its lexicon of contact promises and forbidden requests is conservative and still misses new wordings. Two kinds of reply are still written by the LLM and carry that risk: the closing of a recognized charge and the explanation of a pending duplicate. Source: `docs/declarations.md` 10.2, 10.3.
