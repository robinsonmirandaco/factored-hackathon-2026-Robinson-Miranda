# ADR 7: Autonomy watch with Wilson and an audit sample

- Status: accepted
- Date: Decided in design v4.2, before construction began on 2026-09-25
- Sources: `docs/reports/evaluacion.md` (Autonomy watch [simulado]), `docs/reports/analisis.md` (Autonomy watch diluted by the reviews of handovers), `tests/unit/test_wilson.py`, design section 6.7

## Context

The conformal guarantee holds under exchangeability. If customers start describing charges differently, or the LLM starts reading a variant worse, the guarantee degrades with no signal. The system needs a rule that lowers its autonomy on evidence of errors and gives it back on evidence of recovery, without reacting to noise.

## Decision

- **Cell:** intent × language (es, pt). Each cell has its own level: A0 registers after the customer confirms; A1 needs an analyst's approval; A2 leaves the decision to the analyst, while the system records the action it would have recommended without showing it.
- **Reviews:** an analyst's decision on a case where the system acted or recommended a registration on an identified charge. Audits come from an audit sample: ρ = 0.10 of the cases resolved alone, drawn with a recorded seed. A reversal is a decision that differs, with a reason from a closed list.
- **Rule:** reviews close in blocks of N = 20. At each closed block, r = reversals / N and W is the lower bound of the Wilson interval with z = 1.645. The cell drops one level when W ≥ 0.30 and rises one level after two consecutive blocks with r < 0.15. With N = 20, 10 of 20 gives W ≈ 0.327 and 9 of 20 gives W ≈ 0.284 (`tests/unit/test_wilson.py`).
- Every change of level writes the cell, r, W, N, the threshold crossed and the reversed cases with their reasons to the audit log; the analyst sees it in the Autonomy tab, and the customer sees "in review by an analyst".

## Alternatives considered

- **A fixed threshold on the reversal count** with no interval: reacts to noise with small blocks.
- **Sequential tests (CUSUM, SPRT):** detect sooner but need a model of the error process and more tuning than the evidence available supports.
- **No audit sample:** the watch would see only handovers, never the errors of what the system did alone.

## Consequences

All [simulado]: simulated streams and simulated analysts; nothing measured in production. Source: `docs/reports/evaluacion.md` (Autonomy watch [simulado]); `docs/reports/analisis.md`.

- **Noise:** at a 10% true error, 1 false demotion in 10,000 streams within 10 blocks (criterion fixed before: at most 5%), so N and the thresholds were kept.
- **The desk check of the design was wrong:** at a 40% true error a single block detects in 25.25% of streams (exact 24.47%), not 100%; within 10 blocks, 93.76%; median 60 reviews to detect.
- **The degradation scenario did not degrade** (TRZ-47 CA2, declared as a finding): without its Portuguese example the prompt left PT-BR safe resolution at 67.0% and reversible actions went from 7 to 3; no cell was demoted.
- **Wilson does not stop the error rate the system has:** in the base run, unrecognized charge has about 91 (ES) and 96 (PT) unsafe outcomes per 1,000 cases of the cell, in the resampled streams, that the watch does not stop. Demotion needs a reversal rate near 50% in a block, and the reviews of handovers, almost always right, dilute the audits; counting only audits raises the expected reversal rate to about 15%, still far from it. A lower threshold would be a change to weigh against its false alarms; it was not tuned here.
- The parameters are chosen, not calibrated on real traffic; in production they would be calibrated with real reversals during the shadow stage (design 11.8).
