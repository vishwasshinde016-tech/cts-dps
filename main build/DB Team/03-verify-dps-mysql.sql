-- Read-only verification. Connect with --database=cov_monitoring.
SELECT DATABASE() AS database_name, VERSION() AS mysql_version;
SELECT version_num AS expected_revision
FROM cov_monitoring.dps_alembic_version WHERE version_num = 'f46b72aa08c1';

SELECT COUNT(*) AS expected_24_dps_tables,
       SUM(ENGINE = 'InnoDB' AND TABLE_COLLATION = 'utf8mb4_0900_bin') AS expected_24_with_required_settings
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'cov_monitoring' AND TABLE_TYPE = 'BASE TABLE'
AND TABLE_NAME IN ('dps_ai_cache', 'dps_ai_call', 'dps_ai_route_pause', 'dps_ai_usage', 'dps_alembic_version', 'dps_artifact', 'dps_audit_event', 'dps_audit_seal', 'dps_batch', 'dps_document', 'dps_document_cp_link', 'dps_event_feed', 'dps_golden_candidate', 'dps_job', 'dps_job_attempt', 'dps_label_memory', 'dps_leader', 'dps_push_cursor', 'dps_review_decision', 'dps_run', 'dps_statement_value', 'dps_tenant_limit', 'dps_tool_call', 'dps_trace_event');

SELECT COUNT(*) AS expected_237_dps_columns
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = 'cov_monitoring' AND TABLE_NAME IN ('dps_ai_cache', 'dps_ai_call', 'dps_ai_route_pause', 'dps_ai_usage', 'dps_alembic_version', 'dps_artifact', 'dps_audit_event', 'dps_audit_seal', 'dps_batch', 'dps_document', 'dps_document_cp_link', 'dps_event_feed', 'dps_golden_candidate', 'dps_job', 'dps_job_attempt', 'dps_label_memory', 'dps_leader', 'dps_push_cursor', 'dps_review_decision', 'dps_run', 'dps_statement_value', 'dps_tenant_limit', 'dps_tool_call', 'dps_trace_event');

SELECT EVENT_OBJECT_TABLE, EVENT_MANIPULATION, TRIGGER_NAME
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = 'cov_monitoring' AND ACTION_TIMING = 'BEFORE'
AND EVENT_OBJECT_TABLE IN ('dps_audit_event','dps_review_decision')
AND EVENT_MANIPULATION IN ('UPDATE','DELETE')
ORDER BY EVENT_OBJECT_TABLE, EVENT_MANIPULATION;

SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = 'cov_monitoring' AND TABLE_NAME = 'dps_job'
AND INDEX_NAME IN ('ux_dps_job_idempotency', 'ux_dps_job_scoped_input')
ORDER BY INDEX_NAME, SEQ_IN_INDEX;
