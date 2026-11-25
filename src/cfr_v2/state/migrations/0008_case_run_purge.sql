-- 0008 Purging retired case runs (LDR-0045): the one way rows are ever deleted.
--
-- The append-only trigger gets one exception: a DELETE while the transaction-local setting
-- cfr.purging is on, which only cfr_purge_case_runs sets, and only for the statements that delete a
-- purge's rows. A plain DELETE, an UPDATE or a TRUNCATE still raises.
CREATE OR REPLACE FUNCTION cfr_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('cfr.purging', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'table % is append-only (% rejected)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

-- What was purged, and when: kept for good, never purged itself. Ids, refs and the reason only.
CREATE TABLE case_run_purge (
    case_run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    source_case_id TEXT NOT NULL,
    release_manifest_ref TEXT NOT NULL,
    rerun_of TEXT,
    retired_at TIMESTAMP WITH TIME ZONE NOT NULL,
    purged_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    reason TEXT,
    CONSTRAINT pk_case_run_purge PRIMARY KEY (case_run_id),
    CONSTRAINT ck_case_run_purge_reason CHECK (reason IS NULL OR length(reason) <= 200)
);
CREATE TRIGGER "case_run_purge_append_only" BEFORE UPDATE OR DELETE ON case_run_purge
    FOR EACH ROW EXECUTE FUNCTION cfr_reject_mutation();
CREATE TRIGGER "case_run_purge_no_truncate" BEFORE TRUNCATE ON case_run_purge
    FOR EACH STATEMENT EXECUTE FUNCTION cfr_reject_mutation();

-- Delete retired runs and every row of theirs, in one transaction, and log them. Refuses, naming
-- them, runs that aren't retired, are queued or running, or have re-runs not purged with them.
-- Stored artifacts stay (they're content-addressed, and re-runs share them); the case row stays.
CREATE FUNCTION cfr_purge_case_runs(ids TEXT[], why TEXT) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    runs TEXT[];
    bad TEXT;
    purged INTEGER;
BEGIN
    SELECT array_agg(DISTINCT id) INTO runs FROM unnest(ids) AS id;
    IF runs IS NULL THEN
        RETURN 0;
    END IF;
    PERFORM 1 FROM case_run WHERE case_run_id = ANY(runs) FOR UPDATE;

    SELECT string_agg(id, ', ' ORDER BY id) INTO bad FROM unnest(runs) AS id
        WHERE NOT EXISTS (SELECT 1 FROM case_run_retired x WHERE x.case_run_id = id);
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'only retired runs can be purged; not retired: %', bad USING ERRCODE = 'check_violation';
    END IF;
    SELECT string_agg(case_run_id, ', ' ORDER BY case_run_id) INTO bad FROM workflow_job
        WHERE case_run_id = ANY(runs) AND status IN ('QUEUED', 'RUNNING');
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'queued or running: %', bad USING ERRCODE = 'check_violation';
    END IF;
    SELECT string_agg(DISTINCT r.rerun_of, ', ') INTO bad FROM case_run r
        WHERE r.rerun_of = ANY(runs) AND NOT (r.case_run_id = ANY(runs));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'these have re-runs not purged with them: %', bad USING ERRCODE = 'check_violation';
    END IF;

    INSERT INTO case_run_purge (case_run_id, case_id, source_case_id, release_manifest_ref, rerun_of, retired_at, reason)
    SELECT r.case_run_id, r.case_id, c.source_case_id, r.release_manifest_ref, r.rerun_of, x.retired_at, why
    FROM case_run r JOIN "case" c ON c.case_id = r.case_id JOIN case_run_retired x ON x.case_run_id = r.case_run_id
    WHERE r.case_run_id = ANY(runs);

    PERFORM set_config('cfr.purging', 'on', true);
    DELETE FROM task_attempt WHERE case_run_id = ANY(runs);
    DELETE FROM task_execution WHERE case_run_id = ANY(runs);
    DELETE FROM step_checkpoint WHERE case_run_id = ANY(runs);
    DELETE FROM case_run_event WHERE case_run_id = ANY(runs);
    DELETE FROM workflow_job WHERE case_run_id = ANY(runs);
    DELETE FROM case_run_retirement WHERE case_run_id = ANY(runs);
    DELETE FROM admission WHERE case_run_id = ANY(runs);
    DELETE FROM case_run WHERE case_run_id = ANY(runs);  -- re-runs and their sources together: checked at its end
    GET DIAGNOSTICS purged = ROW_COUNT;
    PERFORM set_config('cfr.purging', 'off', true);
    RETURN purged;
END;
$$;
