# CFR Complaint Intelligence V2

Decision-driven redesign of CFR complaint triage.

Design material is kept alongside the checkout but outside version control
(see `.gitignore`): `hld/` (high-level design), `lld/` (low-level design,
decision records `LDR-NNNN`, roadmap) and `cfr-v1/` (V1 source, reference only).
Code comments cite those documents by path or LDR number.

Current milestone: **Phase 2 (roadmap Phase B) — C01 intake**, complete. Stages C02–C05 are still placeholders.

## Layout

```
src/cfr_v2/
  contracts/    P1  typed contracts + canonical hashing
  state/        P2  case-state schema, Alembic migrations, repository
  artifacts/    P3  content-addressed artifact store (S3 bytes, Postgres metadata)
  llm/          P5  model-client interface, budgets, deterministic fake
  telemetry/    P6  OpenTelemetry / ee-logging facade (ported from V1)
  runtime/      P6  DBOS runtime: case_intake_workflow, lanes, submit
  registry/     P4  release manifests: load, pin, resolve
  intake/       C01 BigQuery case source, normalization, redaction, admission
registry/       release manifests (registry/releases/<id>.yaml)
tests/
  unit/         offline, no network
  telemetry/    ported V1 telemetry suite
  db/           needs docker-compose Postgres (+ RustFS for db/spike)
  _stubs/       stand-ins for private ee-* packages
```

## Setup (WSL2 Ubuntu)

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker Desktop with
WSL integration enabled for this distro (Settings → Resources → WSL Integration).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . --group dev
cp .env.example .env

docker compose up -d          # Postgres 16 (5432) + RustFS S3-compatible store (9000, console 9001)
```

Enterprise-only packages (`safechain`, `ee-*`) install with `-e '.[enterprise]'`
from the internal Artifactory; everything runs without them.

## Database migrations

```bash
set -a; source .env; set +a
.venv/bin/alembic upgrade head
```

## Running intake

```bash
set -a; source .env; set +a
.venv/bin/python -m cfr_v2.intake --synthetic 20                          # offline, synthetic rows
.venv/bin/python -m cfr_v2.intake --start-date 2026-09-01 --end-date 2026-09-07   # BigQuery (enterprise extra)
```

Intake is submit-only: it admits and enqueues. Workflows run in worker processes
(`start_runtime(...)` with the default `listen_lanes`).

## Tests

```bash
.venv/bin/pytest                         # everything; db tests skip if Postgres/RustFS are down
.venv/bin/pytest tests/unit tests/telemetry   # offline only
.venv/bin/pytest tests/db -m "not slow"  # Postgres-backed, excluding crash/throughput spike
.venv/bin/pytest tests/db/spike -s       # DBOS validation spike (LDR-0002), prints throughput
```

Unit tests block outbound network; `db` tests may reach `localhost` only;
`integration` tests are unrestricted and opt-in.
