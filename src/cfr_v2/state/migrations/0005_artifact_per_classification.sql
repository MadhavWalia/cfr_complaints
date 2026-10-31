-- 0005 One artifact row per (sha256, classification) (P3, LDR-0008).
--
-- Identical bytes can be stored under both classifications: a transcript with nothing
-- to redact has redacted text (INTERNAL) == original text (RESTRICTED). The store keeps
-- one object per classification bucket, and reads name the classification, so the
-- metadata row is keyed the same way. Keyed by sha256 alone, the second put failed
-- admission with ArtifactConflictError.
ALTER TABLE artifact DROP CONSTRAINT pk_artifact;
ALTER TABLE artifact ADD CONSTRAINT pk_artifact PRIMARY KEY (sha256, classification);
