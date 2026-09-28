# Language detection (TRZ-11)

> **Development split.** The detector's word lists were tuned on this split for the rules baseline of the comprehension, so these numbers are optimistic. Messages written by hand, and portuñol written on purpose, are only in the held-out test split, which this report does not read.

- Split: dev, 600 cases (150 base cases, 4 variants each)
- Split hash: 15c5f16987d2bfc89020bfdc59a80ea42fe4a307e066937a6fb1239c94773196
- Seed: 42; case config: 1
- Detector: language-rules-1
- LLM variant: claude-haiku-4-5-20251001, prompt comprehension-4, 3 runs from the comprehension cache (no new calls)

## CA5: language accuracy of the detector

Target 95%. Result 99.8%: met.

Rates with their 95% Wilson interval. The four variants of a base case share one message plan, so they are not independent and the intervals are narrower than they should be.

The variant without the LLM comes from the customer's country. The four variants of a base case keep the customer, and so the country, of the base case, so that fallback can match at most one Spanish variant per base case: its low rates for Spanish measure how the cases were built, not how often a customer writes in the variant of their country.

| Group | Detector: language | Turn without LLM: language | Turn without LLM: variant |
| --- | --- | --- | --- |
| all | 99.8% (599/600; 99.1% to 100.0%) | 99.8% (599/600; 99.1% to 100.0%) | 49.8% (299/600; 45.8% to 53.8%) |
| es-MX | 100.0% (150/150; 97.5% to 100.0%) | 100.0% (150/150; 97.5% to 100.0%) | 46.7% (70/150; 38.9% to 54.6%) |
| es-CO | 100.0% (150/150; 97.5% to 100.0%) | 100.0% (150/150; 97.5% to 100.0%) | 32.7% (49/150; 25.7% to 40.5%) |
| es-AR | 100.0% (150/150; 97.5% to 100.0%) | 100.0% (150/150; 97.5% to 100.0%) | 20.7% (31/150; 15.0% to 27.8%) |
| pt-BR | 99.3% (149/150; 96.3% to 99.9%) | 99.3% (149/150; 96.3% to 99.9%) | 99.3% (149/150; 96.3% to 99.9%) |

## CA2: variant with the LLM

Mean over 3 runs, [min, max], (cases). The variant is the LLM's when its language agrees with the detector's; otherwise the country's.

| Group | Turn with LLM: language | Turn with LLM: variant |
| --- | --- | --- |
| all | 100.0% [100.0%, 100.0%] (600) | 64.8% [64.8%, 64.8%] (600) |
| es-MX | 100.0% [100.0%, 100.0%] (150) | 46.7% [46.7%, 46.7%] (150) |
| es-CO | 100.0% [100.0%, 100.0%] (150) | 34.7% [34.7%, 34.7%] (150) |
| es-AR | 100.0% [100.0%, 100.0%] (150) | 78.0% [78.0%, 78.0%] (150) |
| pt-BR | 100.0% [100.0%, 100.0%] (150) | 100.0% [100.0%, 100.0%] (150) |

## First messages

- Ties (no language with more signals; read as Spanish): 3 of 600
- Marked mixed (strong signals of both languages): 5 of 600
- Shorter than 4 words: 0 of 600
