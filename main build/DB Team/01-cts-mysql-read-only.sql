-- Run on the approved CTS MySQL instance. Outputs object metadata and counts only.
SELECT VERSION() AS mysql_version;

SELECT table_schema, table_name, table_type, engine
FROM information_schema.tables
WHERE table_schema IN ('cov_monitoring', 'cov_users')
ORDER BY table_schema, table_name;

SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema IN ('cov_monitoring', 'cov_users')
  AND (LEFT(LOWER(table_name), 4) = 'dps_' OR LOWER(table_name) = 'alembic_version');

-- Compare identifiers within the same CP. A match is evidence about an existing row,
-- while the deployed upload flow still needs a trace to confirm the request mapping.
SELECT COUNT(*) AS certificates,
       COALESCE(SUM(EXISTS (
           SELECT 1 FROM cov_monitoring.cov_document_upload u
           WHERE u.cp_id = c.cp_id AND CAST(u.document_version_id AS BINARY) = CAST(c.file_id AS BINARY)
       )), 0) AS matches_document_version_id,
       COALESCE(SUM(EXISTS (
           SELECT 1 FROM cov_monitoring.cov_document_upload u
           WHERE u.cp_id = c.cp_id AND CAST(u.id AS BINARY) = CAST(c.file_id AS BINARY)
       )), 0) AS matches_upload_row_id,
       COALESCE(SUM(EXISTS (
           SELECT 1 FROM cov_monitoring.cov_document_upload u
           WHERE u.cp_id = c.cp_id AND CAST(u.object_key AS BINARY) = CAST(c.file_id AS BINARY)
       )), 0) AS matches_object_key
FROM cov_monitoring.cov_compliance_certificate c;
