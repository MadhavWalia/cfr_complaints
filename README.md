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
  llm/          P5  model client: gateway (safechain), rate limits, retries, budgets, fake; DSPy engine
  telemetry/    P6  OpenTelemetry / ee-logging facade (from V1), attribute allowlist, dev OTLP export
  runtime/      P6  durable runner on a Postgres job queue: lanes, leases, submit
  registry/     P4  release manifests: load, pin, resolve
  intake/       C01 BigQuery case source, normalization, redaction, admission
  understanding/ C02 Pass 1 (DSPy): allegations, requests and agent statements, with spans
registry/       release manifests (registry/releases/<id>.yaml)
config/         ee-config template (config_gpt.example.yml); your config_gpt.yml is gitignored
tests/
  unit/         offline, no network
  telemetry/    ported V1 telemetry suite
  db/           needs docker-compose Postgres (+ RustFS for db/spike)
  _stubs/       stand-ins for private ee-* packages
```

## Setup (WSL2 Ubuntu)

Requires Python 3.12, either [uv](https://docs.astral.sh/uv/) or conda, and Docker
Desktop with WSL integration enabled for this distro (Settings → Resources → WSL Integration).

**With uv:**

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e . --group dev
```

**With conda:**

```bash
conda create -n cfr-v2 python=3.12 -y
conda activate cfr-v2
python -m pip install -U "pip>=25.1"   # --group needs pip 25.1+
python -m pip install -e . --group dev
```

With conda, run the commands below with `python` / `pytest` in the activated
environment instead of `.venv/bin/python` / `.venv/bin/pytest`.

**Then, either way:**

```bash
cp .env.example .env
cp config/config_gpt.example.yml config/config_gpt.yml   # fill in <...>; enterprise only

docker compose up -d          # Postgres 16 (5432) + RustFS S3-compatible store (9000, console 9001) + dev buckets
```

Enterprise-only packages (`safechain`, `ee-*`) install with `-e '.[enterprise]'`
from the internal Artifactory; everything runs without them.

Behind the corporate TLS-inspecting proxy, export `CFR_TRUSTSTORE=1` so BigQuery,
S3 and the gateway verify against the OS trust store. Nothing loads `.env` by itself:
put it there and `set -a; source .env; set +a`, or prefix the command with it. Don't call
`truststore.inject_into_ssl()` in code: injected after boto3 is imported, it
makes boto's TLS setup recurse until it hits `RecursionError`.

`CONFIG_PATH` (default `config/config_gpt.yml`) names the ee-config file. The
code never passes this path itself: `ee_config.load_config()` reads the variable directly. The
`redaction:`, `idaas:`/`models:` and `elf:` sections are read by ee_utils
redaction, the safechain gateway and ee-logging. The file holds hosts and IDs
per environment, and only the *names* of secret env vars. A relative path
resolves against the working directory, so deployments should set an absolute one.
In dev the ee-* packages are absent and the file is not read.

## Database migrations

```bash
set -a; source .env; set +a
.venv/bin/python -m cfr_v2.state.migrate
```

Run this after every pull that adds a migration. Intake and workers check the schema
at startup and refuse to run with `pending migrations: ...` until it is current.

## Running intake

```bash
set -a; source .env; set +a
.venv/bin/python -m cfr_v2.intake --synthetic 20                          # offline, synthetic rows
.venv/bin/python -m cfr_v2.intake --start-date 2026-09-01 --end-date 2026-09-07   # BigQuery (enterprise extra)
.venv/bin/python -m cfr_v2.intake --print-sql > intake.sql                # the BigQuery job's SQL, e.g. to check its cost
```

One BigQuery job returns the cases and each customer's LUMI baseline (approved account facts,
`new_demographics.sql`). The baseline is stored per case: the model-visible profile as INTERNAL,
the account ref map (`rel_id`, `cm15`) as RESTRICTED. A case without a match is still admitted and
counted under `NO_BASELINE` in the summary. `CFR_INTAKE_BASELINE=0` fetches cases only.

Intake is submit-only: it admits and enqueues. Workflows run in worker processes:

```bash
.venv/bin/python -m cfr_v2.runtime                    # serve every lane until SIGTERM / Ctrl+C
.venv/bin/python -m cfr_v2.runtime --lanes realtime   # one lane only
```

Run as many workers as needed; any worker picks up jobs whose previous worker died.

Workers run C02 Pass 1 for real (gpt-5 through the gateway) and placeholders for C03–C05, so
**starting a worker sends every queued case to the model.** Try Pass 1 on a few cases first:

```bash
python -m cfr_v2.understanding --sample 5 --out pass1_trial   # counts, latency, tokens, cost projection
```

It writes nothing to the state database or the artifact store. The review files in `--out` hold
redacted transcript excerpts: keep them local and delete them after review.

## Model gateway (P5)

Stages call models through `cfr_v2.llm.GatewayModelClient`, which uses V1's access path
(safechain's model for a key in the `models:` section of `CONFIG_PATH`). Each worker process
builds the model once (safechain refreshes its IDaaS token) and runs model calls on one shared
asyncio loop (`model_loop()`), so all cases in the process share one concurrency cap and one
rate-limit cooldown. The model's `redaction: mode:` must be `disabled`: transcripts are already
redacted at intake, and the client refuses a model that would mask them again.

| Failure | Handling |
|---|---|
| 429 | every call in the process pauses (at least the `Retry-After`); doesn't use up attempts |
| timeout, 5xx, 408/409, connection error | retried with jittered backoff |
| 401 | model rebuilt with a fresh token, then retried |
| 403 firewall, content filter (prompt or output), refusal | fails the case at once (`ModelBlockedError`) |
| other 4xx, client bug | fails the case at once |
| output not matching the schema | asked again, then fails the case |
| retries or deadline exhausted | the run is retried later by the worker, not failed |

Limits and timeouts are `CFR_LLM_*` variables; see `.env.example`.

DSPy programs (C02 onwards) reach the gateway through the same client: `cfr_v2.llm.dspy_lm`
plugs a dspy 3.4 custom engine into `dspy.LM`, with DSPy's own retries, cache and history off
(LDR-0015). Run programs with `run_program`, which carries the case identity to each call.

To check the gateway from an enterprise host (a few real calls, synthetic text only):

```bash
python -m cfr_v2.llm.smoke   # model build, plain text, json_schema output, strict schema, trace headers
```

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
