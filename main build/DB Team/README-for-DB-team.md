# Add DPS tables to the existing CTS database

CTS is already connected to MySQL. Use the same **`cov_monitoring`** database in the CTS test environment. The DBA's job is to add the **24 DPS tables**. CTS stays connected as it is, and its existing tables and data stay in place.

## What the DBA does

| Step | File | What to check |
|---|---|---|
| 1. Check | [01-cts-mysql-read-only.sql](01-cts-mysql-read-only.sql) | Confirm the approved CTS test database uses MySQL 8.4. The DPS table check should return no rows. If it finds any, pause and share the output. |
| 2. Create | [02-create-dps-in-cov-monitoring.sql](02-create-dps-in-cov-monitoring.sql) | Connect to `cov_monitoring` and run the file in MySQL batch mode with the default stop-on-error behavior. Keep DPS stopped during this step. |
| 3. Verify | [03-verify-dps-mysql.sql](03-verify-dps-mysql.sql) | Expect **24 tables**, **237 columns**, **4 audit triggers**, and revision **`f46b72aa08c1`**. If a script fails, stop and send its output to the DPS team before retrying. |

Use the approved migration account and the usual backup and change process. The account needs visibility into existing objects and permission to create DPS tables, indexes, foreign keys, and triggers. Use a separate DPS runtime account with access limited to the DPS tables. MySQL commits each schema change separately, so a failed install must be checked before another attempt. [MySQL DDL reference](https://dev.mysql.com/doc/refman/8.4/en/implicit-commit.html)

## What the app teams do next

After the DBA confirms Step 3, deploy the updated MySQL-enabled DPS build and connect it to the same test database using the DPS account. Then run one upload from the existing CTS app and verify that CTS receives the DPS result. Repeat the request with the same key and confirm it returns the same job ID. The CTS developer confirms the document version ID for certificate uploads during this test.

## Validation and reference files

The scripts and the DPS upload flow passed on isolated MySQL 8.4.8. Repeating the same request produced one job and one extraction run. The office database has not been changed.

Use the [CTS table inventory](CTS-dump-schema-inventory.csv) and [DPS column inventory](DPS-expected-MySQL-columns.csv) if you need the schema details.
