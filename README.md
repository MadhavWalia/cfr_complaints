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
  state/        P2  SQL migrations, psycopg/psycopg2 access layer, repository
  artifacts/    P3  content-addressed artifact store (S3 bytes, Postgres metadata)
  llm/          P5  model-client interface, budgets, deterministic fake
  telemetry/    P6  OpenTelemetry / ee-logging facade (from V1), attribute allowlist, dev OTLP export
  runtime/      P6  durable runner on a Postgres job queue: lanes, leases, submit
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
.venv/bin/python -m cfr_v2.state.migrate
```

## Running intake

```bash
set -a; source .env; set +a
.venv/bin/python -m cfr_v2.intake --synthetic 20                          # offline, synthetic rows
.venv/bin/python -m cfr_v2.intake --start-date 2026-09-01 --end-date 2026-09-07   # BigQuery (enterprise extra)
```

Intake is submit-only: it admits and enqueues. Workflows run in worker processes:

```bash
.venv/bin/python -m cfr_v2.runtime                    # serve every lane until SIGTERM / Ctrl+C
.venv/bin/python -m cfr_v2.runtime --lanes realtime   # one lane only
```

Run as many workers as needed; any worker picks up jobs whose previous worker died.

## Telemetry (LDR-0012)

Each case run is one trace: it starts at admission, continues in whichever worker
runs it (including after a crash), and links back to its intake batch.
Enterprise hosts export through `ee-logging` (the `elf:` section of the file named
by `CONFIG_PATH`). In dev, start the local stack and point both processes at it:

```bash
docker compose --profile observability up -d   # Collector (4318), Tempo, Loki, Prometheus, Grafana (3000)
export CFR_OTLP_ENDPOINT=http://localhost:4318
```

Then open Grafana at http://localhost:3000. Span attributes are allowlisted
(`src/cfr_v2/telemetry/attributes.py`) and never carry transcript or redaction-map content.

## Tests

```bash
.venv/bin/pytest                         # everything; db tests skip if Postgres/RustFS are down
.venv/bin/pytest tests/unit tests/telemetry   # offline only
.venv/bin/pytest tests/db -m "not slow"  # Postgres-backed, excluding crash/throughput spike
.venv/bin/pytest tests/db/spike -s       # runtime validation (LDR-0002 criteria), prints throughput
CFR_DB_DRIVER=psycopg2 .venv/bin/pytest tests/db   # same suite on psycopg2
```

Unit tests block outbound network; `db` tests may reach `localhost` only;
`integration` tests are unrestricted and opt-in.
