-- 0004 Trace context on the job row (LDR-0012).
--
-- The W3C traceparent of the run's root span, written in the admission transaction
-- and restored by whichever process claims the job, so every attempt (including one
-- reclaimed after a crash) joins the same trace. NULL for jobs queued before this
-- migration or with tracing off: their attempts start a fresh trace.
ALTER TABLE workflow_job ADD COLUMN trace_context TEXT;
