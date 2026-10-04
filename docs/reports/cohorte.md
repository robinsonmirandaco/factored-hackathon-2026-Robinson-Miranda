# Serving cohort

Customers loaded into the serving database (story TRZ-07, design 9.5).

## Run

- Pipeline version: 1.0.0
- Batch id: 4cd1718d91303ff2
- Reprocessing window: 7 days
- Seed: 42
- Requested size: 5,000
- Population (gold/service_customers): 150,000
- Cohort size: 5,000
- SHA-256 of the sorted cohort customer ids: `dc3f14e9e9c11c03ae267a1fcf65bb9663e864ba4464a903e56a0fe82d927f11`

## Method

- Strata: country (`country_code`) by segment (`segment`) of gold/service_customers.
- Proportional allocation; seats rounded with the largest remainder method, so the
  strata add up to the cohort size exactly.
- Inside a stratum, customers are ordered by md5 of `<seed>:<customer_id>` and the first
  ones are taken. No filter on customer status, products or activity (plan A, TRZ-01).
- Every cohort customer comes with all their products, transactions and complaints;
  exchange rates are loaded whole.

## Country by segment

| Country | Segment | Population | Population share | Cohort | Cohort share | Difference (points) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| AR | Basic | 17,924 | 11.95% | 598 | 11.96% | +0.01 |
| AR | Plus | 7,441 | 4.96% | 248 | 4.96% | +0.00 |
| AR | Premium | 2,979 | 1.99% | 99 | 1.98% | -0.01 |
| AR | Student | 1,498 | 1.00% | 50 | 1.00% | +0.00 |
| CO | Basic | 27,069 | 18.05% | 902 | 18.04% | -0.01 |
| CO | Plus | 11,309 | 7.54% | 377 | 7.54% | +0.00 |
| CO | Premium | 4,584 | 3.06% | 153 | 3.06% | +0.00 |
| CO | Student | 2,289 | 1.53% | 76 | 1.52% | -0.01 |
| MX | Basic | 44,763 | 29.84% | 1,492 | 29.84% | +0.00 |
| MX | Plus | 18,797 | 12.53% | 627 | 12.54% | +0.01 |
| MX | Premium | 7,644 | 5.10% | 255 | 5.10% | +0.00 |
| MX | Student | 3,703 | 2.47% | 123 | 2.46% | -0.01 |

## By country

| Country | Population | Population share | Cohort | Cohort share | Difference (points) |
| --- | ---: | ---: | ---: | ---: | ---: |
| AR | 29,842 | 19.89% | 995 | 19.90% | +0.01 |
| CO | 45,251 | 30.17% | 1,508 | 30.16% | -0.01 |
| MX | 74,907 | 49.94% | 2,497 | 49.94% | +0.00 |

## By segment

| Segment | Population | Population share | Cohort | Cohort share | Difference (points) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 59.84% | 2,992 | 59.84% | +0.00 |
| Plus | 37,547 | 25.03% | 1,252 | 25.04% | +0.01 |
| Premium | 15,207 | 10.14% | 507 | 10.14% | +0.00 |
| Student | 7,490 | 4.99% | 249 | 4.98% | -0.01 |

Largest difference against the population: 0.01 points by stratum, 0.01 by country, 0.01 by segment (limit: 2 points).

## Rows per table

Source: the gold serving tables, filtered to the cohort customers (`gold/cohort/`).

| Table | Rows |
| --- | ---: |
| customers | 5,000 |
| products | 13,138 |
| transactions | 144,475 |
| complaints | 2,285 |
| exchange_rates | 13,164 |

Cohort customers with no product: 388. With no transaction: 549.

## Evaluation splits

The splits of TRZ-42 are drawn from gold/cohort/customers.parquet, so every customer
they name is loaded. `pipeline.cohort.missing_from_cohort` is the check, run by the
unit tests.
