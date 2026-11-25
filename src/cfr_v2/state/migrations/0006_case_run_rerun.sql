-- 0006 Re-runs (LDR-0045): a new case run of a case already run, under the current release, that
-- reuses the source run's results for the stages before ``rerun_from`` and runs the rest.
-- A case still has one admitted run per ingestion revision (intake's duplicate check); a re-run is
-- unique per source run, release and stage, so asking for it twice queues it once.
ALTER TABLE case_run ADD COLUMN rerun_of TEXT;
ALTER TABLE case_run ADD COLUMN rerun_from TEXT;
ALTER TABLE case_run ADD CONSTRAINT fk_case_run_rerun_of_case_run
    FOREIGN KEY (rerun_of) REFERENCES case_run (case_run_id);
ALTER TABLE case_run ADD CONSTRAINT ck_case_run_rerun CHECK ((rerun_of IS NULL) = (rerun_from IS NULL));

ALTER TABLE case_run DROP CONSTRAINT uq_case_run_case_id_ingestion_revision;
CREATE UNIQUE INDEX uq_case_run_admitted ON case_run (case_id, ingestion_revision) WHERE rerun_of IS NULL;
CREATE UNIQUE INDEX uq_case_run_rerun ON case_run (rerun_of, release_manifest_ref, rerun_from)
    WHERE rerun_of IS NOT NULL;
