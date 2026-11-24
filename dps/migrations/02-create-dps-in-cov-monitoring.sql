-- DPS MySQL 8.4 bootstrap. Connect with --database=cov_monitoring.
-- Run with the mysql client in batch mode, with its default stop-on-error behavior.
-- Never use --force. MySQL commits each DDL statement individually.

SET @dps_target_ok = CAST(DATABASE() AS BINARY) = CAST('cov_monitoring' AS BINARY);
SET @dps_lock = IF(@dps_target_ok, GET_LOCK('cov_monitoring.dps_schema_install', 0), 0);

SET @dps_preflight = CASE
    WHEN COALESCE(@dps_target_ok, 0) <> 1
        THEN 'wrong_database'
    WHEN SUBSTRING_INDEX(VERSION(), '.', 2) <> '8.4' THEN 'unexpected_mysql_version'
    WHEN COALESCE(@dps_lock, 0) <> 1
        THEN 'bootstrap_locked'
    WHEN EXISTS (
        SELECT 1 FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND LEFT(LOWER(TABLE_NAME), 4) = 'dps_'
    ) OR EXISTS (
        SELECT 1 FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = DATABASE() AND LEFT(LOWER(TRIGGER_NAME), 4) = 'dps_'
    ) THEN 'existing_or_partial_dps_install'
    ELSE 'ready'
END;
SELECT @dps_preflight AS dps_bootstrap_preflight;
-- PREPARE from NULL fails and stops the batch before any DDL on an invalid target.
SET @dps_guard_sql = IF(@dps_preflight = 'ready', 'DO 0', NULL);
PREPARE dps_guard FROM @dps_guard_sql;
EXECUTE dps_guard;
DEALLOCATE PREPARE dps_guard;

CREATE TABLE dps_ai_cache (
	`key` VARCHAR(64) NOT NULL,
	model VARCHAR(128) NOT NULL,
	answer LONGTEXT NOT NULL,
	hits INTEGER NOT NULL,
	created_at DATETIME(6) NOT NULL,
	last_used_at DATETIME(6) NOT NULL,
	PRIMARY KEY (`key`)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_ai_call (
	id VARCHAR(36) NOT NULL,
	at DATETIME(6) NOT NULL,
	purpose VARCHAR(24) NOT NULL,
	user VARCHAR(255) NOT NULL,
	cp_id VARCHAR(64),
	document_id VARCHAR(36),
	job_id VARCHAR(36),
	attempt_no INTEGER,
	request_id VARCHAR(64),
	model VARCHAR(128) NOT NULL,
	prompt_version VARCHAR(16) NOT NULL,
	status VARCHAR(8) NOT NULL,
	error VARCHAR(500),
	seconds DOUBLE NOT NULL,
	prompt_tokens INTEGER NOT NULL,
	completion_tokens INTEGER NOT NULL,
	cost DOUBLE,
	request_text LONGTEXT,
	response_text LONGTEXT,
	trace_id VARCHAR(32),
	span_id VARCHAR(16),
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_ai_call_at ON dps_ai_call (at);

CREATE INDEX ix_dps_ai_call_document ON dps_ai_call (document_id);

CREATE INDEX ix_dps_ai_call_trace ON dps_ai_call (trace_id);

CREATE TABLE dps_ai_route_pause (
	route VARCHAR(64) NOT NULL,
	paused_at DATETIME(6) NOT NULL,
	PRIMARY KEY (route)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_ai_usage (
	id VARCHAR(36) NOT NULL,
	at DATETIME(6) NOT NULL,
	purpose VARCHAR(24) NOT NULL,
	user VARCHAR(255) NOT NULL,
	cp_id VARCHAR(64),
	document_id VARCHAR(36),
	job_id VARCHAR(36),
	model VARCHAR(128) NOT NULL,
	calls INTEGER NOT NULL,
	prompt_tokens INTEGER NOT NULL,
	completion_tokens INTEGER NOT NULL,
	cost DOUBLE,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_ai_usage_at ON dps_ai_usage (at);

CREATE TABLE dps_artifact (
	id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(64) NOT NULL,
	version_id VARCHAR(64) NOT NULL,
	kind VARCHAR(24) NOT NULL,
	path VARCHAR(400) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	bytes INTEGER NOT NULL,
	source_sha256 VARCHAR(64),
	pipeline_version VARCHAR(32),
	created_at DATETIME(6) NOT NULL,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_artifact_source ON dps_artifact (source_sha256, kind, pipeline_version);

CREATE UNIQUE INDEX ux_dps_artifact_path ON dps_artifact (path);

CREATE TABLE dps_audit_event (
	id VARCHAR(36) NOT NULL,
	actor VARCHAR(255) NOT NULL,
	action VARCHAR(64) NOT NULL,
	target_type VARCHAR(32) NOT NULL,
	target_id VARCHAR(64) NOT NULL,
	detail_json LONGTEXT NOT NULL,
	at DATETIME(6) NOT NULL,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_audit_seal (
	day DATE NOT NULL,
	`rows` INTEGER NOT NULL,
	prev_hash VARCHAR(64) NOT NULL,
	head_hash VARCHAR(64) NOT NULL,
	file_sha256 VARCHAR(64),
	created_at DATETIME(6) NOT NULL,
	PRIMARY KEY (day)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_batch (
	id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(64) NOT NULL,
	batch_ref VARCHAR(128) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL,
	item_count INTEGER NOT NULL,
	created_by VARCHAR(255) NOT NULL,
	created_at DATETIME(6) NOT NULL,
	finished_at DATETIME(6),
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE UNIQUE INDEX ux_dps_batch_idempotency ON dps_batch (tenant_id, idempotency_key);

CREATE TABLE dps_document (
	id VARCHAR(36) NOT NULL,
	type VARCHAR(32) NOT NULL,
	file_name VARCHAR(255) NOT NULL,
	file_id VARCHAR(255),
	storage_path VARCHAR(500) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	size_bytes INTEGER NOT NULL,
	page_count INTEGER,
	as_of_date DATE,
	status VARCHAR(24) NOT NULL,
	uploaded_by VARCHAR(255) NOT NULL,
	uploaded_at DATETIME(6) NOT NULL,
	latest_job_id VARCHAR(36),
	amends_document_id VARCHAR(36),
	tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
	external_version_id VARCHAR(64),
	object_key VARCHAR(500),
	review_json LONGTEXT,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_document_external_version_id ON dps_document (external_version_id);

CREATE INDEX ix_dps_document_sha256 ON dps_document (sha256);

CREATE TABLE dps_event_feed (
	seq INTEGER NOT NULL AUTO_INCREMENT,
	event_id VARCHAR(36) NOT NULL,
	type VARCHAR(48) NOT NULL,
	tenant_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(36),
	document_version_id VARCHAR(64),
	correlation_id VARCHAR(128),
	occurred_at DATETIME(6) NOT NULL,
	payload_json LONGTEXT NOT NULL,
	PRIMARY KEY (seq),
	UNIQUE (event_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_golden_candidate (
	id VARCHAR(36) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	field_id VARCHAR(64) NOT NULL,
	job_type VARCHAR(32) NOT NULL,
	payload_json LONGTEXT NOT NULL,
	status VARCHAR(16) NOT NULL,
	decided_by VARCHAR(255),
	created_at DATETIME(6) NOT NULL,
	decided_at DATETIME(6),
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_job_attempt (
	id VARCHAR(36) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	attempt_no INTEGER NOT NULL,
	worker_id VARCHAR(64) NOT NULL,
	started_at DATETIME(6) NOT NULL,
	ended_at DATETIME(6),
	outcome VARCHAR(24),
	error_code VARCHAR(48),
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_job_attempt_job_id ON dps_job_attempt (job_id);

CREATE TABLE dps_label_memory (
	id VARCHAR(36) NOT NULL,
	cp_id VARCHAR(64) NOT NULL,
	label VARCHAR(300) NOT NULL,
	`key` VARCHAR(64) NOT NULL,
	source_label VARCHAR(300) NOT NULL,
	document_id VARCHAR(36),
	confirmed_by VARCHAR(255) NOT NULL,
	created_at DATETIME(6) NOT NULL,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE UNIQUE INDEX ux_dps_label_memory ON dps_label_memory (cp_id, label);

CREATE TABLE dps_leader (
	name VARCHAR(32) NOT NULL,
	holder VARCHAR(64) NOT NULL,
	until DATETIME(6) NOT NULL,
	PRIMARY KEY (name)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_push_cursor (
	name VARCHAR(32) NOT NULL,
	seq INTEGER NOT NULL,
	updated_at DATETIME(6) NOT NULL,
	PRIMARY KEY (name)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_review_decision (
	id VARCHAR(36) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	run_no INTEGER NOT NULL,
	field_id VARCHAR(64) NOT NULL,
	reviewer VARCHAR(255) NOT NULL,
	action VARCHAR(16) NOT NULL,
	old_value_json LONGTEXT,
	new_value_json LONGTEXT,
	reason_code VARCHAR(32),
	comment VARCHAR(2000),
	sampled BOOL NOT NULL,
	at DATETIME(6) NOT NULL,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_review_decision_job_id ON dps_review_decision (job_id);

CREATE TABLE dps_run (
	id VARCHAR(36) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	run_no INTEGER NOT NULL,
	attempt_no INTEGER,
	base_run_id VARCHAR(36),
	status VARCHAR(16) NOT NULL,
	`trigger` VARCHAR(24) NOT NULL,
	result_json LONGTEXT NOT NULL,
	superseded_by VARCHAR(64),
	created_at DATETIME(6) NOT NULL,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE UNIQUE INDEX ux_dps_run_job_no ON dps_run (job_id, run_no);

CREATE TABLE dps_statement_value (
	id VARCHAR(36) NOT NULL,
	cp_id VARCHAR(64) NOT NULL,
	`key` VARCHAR(64) NOT NULL,
	value DOUBLE NOT NULL,
	unit VARCHAR(32) NOT NULL,
	currency VARCHAR(3),
	as_of_date DATE NOT NULL,
	period_months INTEGER,
	derived BOOL NOT NULL,
	source_type VARCHAR(16) NOT NULL,
	document_id VARCHAR(36),
	page INTEGER,
	cell VARCHAR(64),
	note VARCHAR(2000),
	entered_by VARCHAR(255),
	superseded BOOL NOT NULL,
	created_at DATETIME(6) NOT NULL,
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_value_lookup ON dps_statement_value (cp_id, `key`, as_of_date, superseded);

CREATE TABLE dps_tenant_limit (
	tenant_id VARCHAR(64) NOT NULL,
	max_queued INTEGER NOT NULL,
	max_running INTEGER NOT NULL,
	max_running_per_batch INTEGER NOT NULL,
	PRIMARY KEY (tenant_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE dps_tool_call (
	id VARCHAR(36) NOT NULL,
	at DATETIME(6) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	attempt_no INTEGER,
	tool VARCHAR(64) NOT NULL,
	request_sha256 VARCHAR(64) NOT NULL,
	response_sha256 VARCHAR(64),
	error_type VARCHAR(96),
	spring_version VARCHAR(128),
	trace_id VARCHAR(32),
	read_span_id VARCHAR(16),
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_tool_call_job_id ON dps_tool_call (job_id);

CREATE TABLE dps_trace_event (
	id VARCHAR(36) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	job_id VARCHAR(36),
	at DATETIME(6) NOT NULL,
	step VARCHAR(16) NOT NULL,
	actor VARCHAR(255) NOT NULL,
	summary VARCHAR(1000) NOT NULL,
	detail_json LONGTEXT NOT NULL,
	trace_id VARCHAR(32),
	PRIMARY KEY (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_trace_document ON dps_trace_event (document_id, at);

CREATE INDEX ix_dps_trace_event_trace ON dps_trace_event (trace_id);

CREATE TABLE dps_document_cp_link (
	id VARCHAR(36) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	cp_id VARCHAR(64) NOT NULL,
	linked_by VARCHAR(255) NOT NULL,
	linked_at DATETIME(6) NOT NULL,
	unlinked_by VARCHAR(255),
	unlinked_at DATETIME(6),
	PRIMARY KEY (id),
	FOREIGN KEY(document_id) REFERENCES dps_document (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_link_cp ON dps_document_cp_link (cp_id);

CREATE TABLE dps_job (
	id VARCHAR(36) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	flow VARCHAR(32) NOT NULL,
	status VARCHAR(16) NOT NULL,
	pool VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	current_step VARCHAR(32),
	error LONGTEXT,
	steps_json LONGTEXT NOT NULL,
	locked_by VARCHAR(64),
	locked_until DATETIME(6),
	created_at DATETIME(6) NOT NULL,
	updated_at DATETIME(6),
	job_type VARCHAR(32) NOT NULL DEFAULT 'EXTRACT_AGREEMENT',
	tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
	priority INTEGER NOT NULL DEFAULT '1',
	due_at DATETIME(6),
	idempotency_key VARCHAR(128),
	request_hash VARCHAR(64),
	input_key VARCHAR(64),
	request_json LONGTEXT,
	batch_id VARCHAR(36),
	client_file_reference VARCHAR(128),
	correlation_id VARCHAR(128),
	cancel_requested BOOL NOT NULL DEFAULT false,
	started_at DATETIME(6),
	finished_at DATETIME(6),
	error_code VARCHAR(48),
	pipeline_version VARCHAR(32) NOT NULL DEFAULT '1',
	rollout VARCHAR(16) NOT NULL DEFAULT 'NORMAL',
	document_version_id VARCHAR(64),
	sha256 VARCHAR(64),
	crash_count INTEGER NOT NULL DEFAULT '0',
	requested_by VARCHAR(255),
	PRIMARY KEY (id),
	FOREIGN KEY(document_id) REFERENCES dps_document (id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE INDEX ix_dps_job_batch_id ON dps_job (batch_id);

CREATE INDEX ix_dps_job_claim ON dps_job (pool, status, priority, due_at);

CREATE INDEX ix_dps_job_correlation ON dps_job (correlation_id);

CREATE INDEX ix_dps_job_document ON dps_job (document_id);

CREATE INDEX ix_dps_job_version ON dps_job (tenant_id, document_version_id);

CREATE UNIQUE INDEX ux_dps_job_idempotency ON dps_job (tenant_id, idempotency_key);

CREATE UNIQUE INDEX ux_dps_job_scoped_input ON dps_job (tenant_id, input_key);

CREATE TRIGGER dps_audit_event_no_update BEFORE UPDATE ON dps_audit_event
FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'DPS audit history is append-only.';

CREATE TRIGGER dps_audit_event_no_delete BEFORE DELETE ON dps_audit_event
FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'DPS audit history is append-only.';

CREATE TRIGGER dps_review_decision_no_update BEFORE UPDATE ON dps_review_decision
FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'DPS audit history is append-only.';

CREATE TRIGGER dps_review_decision_no_delete BEFORE DELETE ON dps_review_decision
FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'DPS audit history is append-only.';

CREATE TABLE dps_alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)
ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
INSERT INTO dps_alembic_version (version_num) VALUES ('f46b72aa08c1');
SELECT RELEASE_LOCK('cov_monitoring.dps_schema_install') AS dps_bootstrap_released;
