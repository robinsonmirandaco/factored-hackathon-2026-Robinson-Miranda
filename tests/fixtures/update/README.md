# Update fixture (test data, not real data)

Every value in this folder is made up for tests. Nothing here comes from the LATAM Bank dataset.

The dataset is a static export, so a new delivery never arrives. These files simulate two
deliveries so the incremental load can be tested (story TRZ-05, design §9.4). They follow the
same partition layout as the S3 export and the synthetic base built by `tests/pipeline_data.py`
(customers `CUS-1` to `CUS-3`, products `PRD-1` to `PRD-3`).

| Folder | Simulates | Expected result |
| --- | --- | --- |
| `late_partition/` | `transactions` for 2024-01-05, delivered after later days were already loaded and outside the reprocessing window | Its three rows enter silver once; loading it again adds nothing |
| `new_column/` | `complaints` for 2024-01-12 with an extra column, `escalation_level` | The whole partition goes to quarantine with `schema_mismatch` |

`tests/integration/test_update_fixture.py` copies each folder into the raw layer of a test data
directory and checks those results.
