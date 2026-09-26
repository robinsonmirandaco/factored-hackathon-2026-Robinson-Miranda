# Candidate density

Disputable transactions per customer in the dispute window (story TRZ-01, design §17).

## Run

- Run at (UTC): 2026-09-26T17:18:31
- Seed: 42
- TRAZO_NOW reference date: 2026-06-17
- Random reference dates (seed 42): 2023-11-08, 2024-02-05, 2024-07-21, 2025-07-29, 2025-11-11
- Transaction partitions read: 1,097 (2023-06-17 to 2026-06-17)
- Transaction rows read: 4,425,008
- Disputable rows over the whole period: 2,619,413
- Transaction rows with a customer_id not in customers.csv: 0
- Customers in customers.csv: 150,000

## Definitions

- Disputable: `transaction_type` in Purchase, Payment, Withdrawal and `transaction_status` in Approved, Pending.
- Window: the 120 calendar days that end on the reference date, both included, by partition date (the local business day). `transaction_date` runs up to a few hours past its partition day, so it is not used for the window.
- Random reference dates are drawn so the whole window falls inside the data period; the TRAZO_NOW date is excluded from the draw.
- Every customer in customers.csv is counted, including those with zero candidates.
- p50 and p90 are discrete quantiles (an observed value). Percentages are rounded to one decimal with the largest remainder method, so each row sums to exactly 100%.

## All customers by reference date

| Reference date | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-06-17 | 150,000 | 1.94 | 1 | 5 | 17 | 28.9% (43,386) | 21.9% (32,788) | 30.2% (45,310) | 19.0% (28,516) |
| 2023-11-08 | 150,000 | 1.91 | 1 | 5 | 17 | 29.1% (43,709) | 22.2% (33,306) | 30.1% (45,117) | 18.6% (27,868) |
| 2024-02-05 | 150,000 | 1.92 | 1 | 5 | 18 | 29.2% (43,861) | 21.9% (32,820) | 30.2% (45,291) | 18.7% (28,028) |
| 2024-07-21 | 150,000 | 1.92 | 1 | 5 | 16 | 29.2% (43,717) | 22.0% (33,005) | 30.3% (45,503) | 18.5% (27,775) |
| 2025-07-29 | 150,000 | 1.92 | 1 | 5 | 16 | 29.1% (43,616) | 22.0% (33,058) | 30.2% (45,355) | 18.7% (27,971) |
| 2025-11-11 | 150,000 | 1.92 | 1 | 5 | 17 | 29.1% (43,610) | 22.0% (32,985) | 30.4% (45,608) | 18.5% (27,797) |

## 2026-06-17 (TRAZO_NOW)

| Total | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All customers | 150,000 | 1.94 | 1 | 5 | 17 | 28.9% (43,386) | 21.9% (32,788) | 30.2% (45,310) | 19.0% (28,516) |

| Country | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina | 29,842 | 1.95 | 1 | 5 | 17 | 28.8% (8,609) | 21.8% (6,492) | 30.3% (9,048) | 19.1% (5,693) |
| Colombia | 45,251 | 1.94 | 1 | 5 | 16 | 29.1% (13,165) | 21.9% (9,917) | 30.1% (13,634) | 18.9% (8,535) |
| México | 74,907 | 1.95 | 1 | 5 | 16 | 28.8% (21,612) | 21.9% (16,379) | 30.2% (22,628) | 19.1% (14,288) |

| Segment | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 1.94 | 1 | 5 | 16 | 28.9% (25,903) | 21.8% (19,588) | 30.4% (27,252) | 18.9% (17,013) |
| Plus | 37,547 | 1.95 | 1 | 5 | 16 | 28.8% (10,819) | 21.8% (8,169) | 30.3% (11,369) | 19.1% (7,190) |
| Premium | 15,207 | 1.92 | 1 | 5 | 13 | 29.3% (4,459) | 22.0% (3,348) | 29.7% (4,516) | 19.0% (2,884) |
| Student | 7,490 | 1.94 | 1 | 5 | 17 | 29.4% (2,205) | 22.5% (1,683) | 29.0% (2,173) | 19.1% (1,429) |

## 2023-11-08 (random date)

| Total | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All customers | 150,000 | 1.91 | 1 | 5 | 17 | 29.1% (43,709) | 22.2% (33,306) | 30.1% (45,117) | 18.6% (27,868) |

| Country | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina | 29,842 | 1.91 | 1 | 5 | 15 | 29.5% (8,803) | 22.0% (6,574) | 30.0% (8,945) | 18.5% (5,520) |
| Colombia | 45,251 | 1.91 | 1 | 5 | 16 | 29.1% (13,157) | 22.2% (10,069) | 30.1% (13,626) | 18.6% (8,399) |
| México | 74,907 | 1.92 | 1 | 5 | 17 | 29.0% (21,749) | 22.3% (16,663) | 30.1% (22,546) | 18.6% (13,949) |

| Segment | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 1.91 | 1 | 5 | 16 | 29.1% (26,082) | 22.3% (19,996) | 30.1% (27,039) | 18.5% (16,639) |
| Plus | 37,547 | 1.92 | 1 | 5 | 17 | 29.1% (10,936) | 22.1% (8,291) | 30.0% (11,277) | 18.8% (7,043) |
| Premium | 15,207 | 1.90 | 1 | 5 | 14 | 29.5% (4,479) | 22.3% (3,385) | 29.8% (4,538) | 18.4% (2,805) |
| Student | 7,490 | 1.90 | 1 | 5 | 15 | 29.5% (2,212) | 21.8% (1,634) | 30.2% (2,263) | 18.5% (1,381) |

## 2024-02-05 (random date)

| Total | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All customers | 150,000 | 1.92 | 1 | 5 | 18 | 29.2% (43,861) | 21.9% (32,820) | 30.2% (45,291) | 18.7% (28,028) |

| Country | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina | 29,842 | 1.90 | 1 | 5 | 18 | 29.7% (8,848) | 21.8% (6,512) | 30.2% (9,018) | 18.3% (5,464) |
| Colombia | 45,251 | 1.92 | 1 | 5 | 17 | 29.3% (13,274) | 21.8% (9,854) | 30.2% (13,660) | 18.7% (8,463) |
| México | 74,907 | 1.93 | 1 | 5 | 16 | 29.0% (21,739) | 22.0% (16,454) | 30.2% (22,613) | 18.8% (14,101) |

| Segment | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 1.92 | 1 | 5 | 17 | 29.2% (26,244) | 21.9% (19,659) | 30.2% (27,101) | 18.7% (16,752) |
| Plus | 37,547 | 1.94 | 1 | 5 | 18 | 29.0% (10,897) | 22.0% (8,235) | 30.1% (11,312) | 18.9% (7,103) |
| Premium | 15,207 | 1.92 | 1 | 5 | 14 | 29.2% (4,440) | 21.8% (3,323) | 30.6% (4,653) | 18.4% (2,791) |
| Student | 7,490 | 1.89 | 1 | 5 | 16 | 30.4% (2,280) | 21.4% (1,603) | 29.7% (2,225) | 18.5% (1,382) |

## 2024-07-21 (random date)

| Total | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All customers | 150,000 | 1.92 | 1 | 5 | 16 | 29.2% (43,717) | 22.0% (33,005) | 30.3% (45,503) | 18.5% (27,775) |

| Country | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina | 29,842 | 1.93 | 1 | 5 | 15 | 29.3% (8,755) | 21.9% (6,526) | 29.8% (8,883) | 19.0% (5,678) |
| Colombia | 45,251 | 1.91 | 1 | 5 | 16 | 29.3% (13,261) | 21.8% (9,842) | 30.7% (13,898) | 18.2% (8,250) |
| México | 74,907 | 1.92 | 1 | 5 | 16 | 29.0% (21,701) | 22.2% (16,637) | 30.3% (22,722) | 18.5% (13,847) |

| Segment | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 1.92 | 1 | 5 | 16 | 29.2% (26,207) | 21.8% (19,607) | 30.3% (27,173) | 18.7% (16,769) |
| Plus | 37,547 | 1.92 | 1 | 5 | 15 | 28.9% (10,837) | 22.3% (8,363) | 30.4% (11,426) | 18.4% (6,921) |
| Premium | 15,207 | 1.89 | 1 | 5 | 16 | 29.3% (4,462) | 22.3% (3,387) | 30.8% (4,679) | 17.6% (2,679) |
| Student | 7,490 | 1.92 | 1 | 5 | 16 | 29.5% (2,211) | 22.0% (1,648) | 29.7% (2,225) | 18.8% (1,406) |

## 2025-07-29 (random date)

| Total | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All customers | 150,000 | 1.92 | 1 | 5 | 16 | 29.1% (43,616) | 22.0% (33,058) | 30.2% (45,355) | 18.7% (27,971) |

| Country | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina | 29,842 | 1.93 | 1 | 5 | 16 | 29.1% (8,670) | 22.0% (6,577) | 30.0% (8,967) | 18.9% (5,628) |
| Colombia | 45,251 | 1.90 | 1 | 5 | 15 | 29.2% (13,240) | 22.3% (10,076) | 30.3% (13,709) | 18.2% (8,226) |
| México | 74,907 | 1.92 | 1 | 5 | 15 | 29.0% (21,706) | 21.9% (16,405) | 30.3% (22,679) | 18.8% (14,117) |

| Segment | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 1.92 | 1 | 5 | 15 | 28.9% (25,988) | 22.0% (19,744) | 30.4% (27,256) | 18.7% (16,768) |
| Plus | 37,547 | 1.92 | 1 | 5 | 15 | 29.0% (10,897) | 22.3% (8,366) | 30.1% (11,295) | 18.6% (6,989) |
| Premium | 15,207 | 1.91 | 1 | 5 | 16 | 29.5% (4,486) | 21.8% (3,310) | 30.1% (4,583) | 18.6% (2,828) |
| Student | 7,490 | 1.90 | 1 | 5 | 14 | 30.0% (2,245) | 21.9% (1,638) | 29.6% (2,221) | 18.5% (1,386) |

## 2025-11-11 (random date)

| Total | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All customers | 150,000 | 1.92 | 1 | 5 | 17 | 29.1% (43,610) | 22.0% (32,985) | 30.4% (45,608) | 18.5% (27,797) |

| Country | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Argentina | 29,842 | 1.91 | 1 | 5 | 17 | 29.4% (8,769) | 21.8% (6,521) | 30.3% (9,031) | 18.5% (5,521) |
| Colombia | 45,251 | 1.92 | 1 | 5 | 17 | 29.1% (13,165) | 22.1% (10,008) | 30.3% (13,728) | 18.5% (8,350) |
| México | 74,907 | 1.92 | 1 | 5 | 16 | 28.9% (21,676) | 22.0% (16,456) | 30.5% (22,849) | 18.6% (13,926) |

| Segment | Customers | Mean | p50 | p90 | Max | 0 candidates | 1 candidates | 2-3 candidates | >3 candidates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Basic | 89,756 | 1.92 | 1 | 5 | 16 | 28.9% (25,944) | 22.0% (19,781) | 30.4% (27,233) | 18.7% (16,798) |
| Plus | 37,547 | 1.91 | 1 | 5 | 17 | 29.3% (10,990) | 21.9% (8,211) | 30.5% (11,467) | 18.3% (6,879) |
| Premium | 15,207 | 1.90 | 1 | 5 | 14 | 29.3% (4,455) | 22.1% (3,360) | 30.3% (4,609) | 18.3% (2,783) |
| Student | 7,490 | 1.89 | 1 | 5 | 15 | 29.7% (2,221) | 21.8% (1,633) | 30.7% (2,299) | 17.8% (1,337) |
