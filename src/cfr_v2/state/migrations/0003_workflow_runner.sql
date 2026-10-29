-- 0003 Scheduling state for the in-house durable runner (LDR-0011).
--
-- workflow_job is the one table updated in place: it says what should run next and
-- who holds it, never what a case concluded. Results stay in the append-only
-- tables (task_execution, case_run_event, ...).
CREATE TABLE workflow_job (
    case_run_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    run_after TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lease_owner TEXT,
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_workflow_job PRIMARY KEY (case_run_id),
    CONSTRAINT fk_workflow_job_case_run_id_case_run FOREIGN KEY (case_run_id) REFERENCES case_run (case_run_id),
    CONSTRAINT ck_workflow_job_lane CHECK (lane IN ('batch', 'realtime')),
    CONSTRAINT ck_workflow_job_status CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_workflow_job_lease CHECK ((status = 'RUNNING') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);

-- Claim paths: queued work that is due, and running work whose lease expired.
CREATE INDEX ix_workflow_job_queued ON workflow_job (lane, run_after) WHERE status = 'QUEUED';
CREATE INDEX ix_workflow_job_running ON workflow_job (lane, lease_expires_at) WHERE status = 'RUNNING';

-- Output of a non-transactional step (e.g. a model call plus its S3 upload), so a
-- recovered run reuses it instead of producing again. Append-only.
CREATE TABLE step_checkpoint (
    case_run_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    output JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_step_checkpoint PRIMARY KEY (case_run_id, step_name),
    CONSTRAINT fk_step_checkpoint_case_run_id_case_run FOREIGN KEY (case_run_id) REFERENCES case_run (case_run_id)
);

CREATE TRIGGER "step_checkpoint_append_only" BEFORE UPDATE OR DELETE ON step_checkpoint
    FOR EACH ROW EXECUTE FUNCTION cfr_reject_mutation();
CREATE TRIGGER "step_checkpoint_no_truncate" BEFORE TRUNCATE ON step_checkpoint
    FOR EACH STATEMENT EXECUTE FUNCTION cfr_reject_mutation();
