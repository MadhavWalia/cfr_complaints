-- Runs once, on first start of an empty data volume.
-- cfr_state: case state (P2) and artifact metadata (P3).
-- cfr_dbos:  DBOS system database (workflow and step checkpoints, queues).
CREATE DATABASE cfr_state OWNER cfr;
CREATE DATABASE cfr_dbos OWNER cfr;
