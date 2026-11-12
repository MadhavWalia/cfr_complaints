# CFR Complaint Intelligence V2

Decision-driven redesign of CFR complaint triage.

Design material is kept alongside the checkout but outside version control
(see `.gitignore`): `hld/` (high-level design), `lld/` (low-level design,
decision records `LDR-NNNN`, roadmap) and `cfr-v1/` (V1 source, reference only).
Code comments cite those documents by path or LDR number.

Current milestone: **Phase 5 (roadmap Phase E) — C04 router**. Intake (C01), understanding (C02),
primary selection (C03) and routing (C04) run in the worker; planning (C05) is still a placeholder.

## Layout

```
src/cfr_v2/
  contracts/    P1  typed contracts + canonical hashing
  state/        P2  SQL migrations, psycopg/psycopg2 access layer, repository
  artifacts/    P3  content-addressed artifact store (S3 bytes, Postgres metadata)
  llm/          P5  model client: gateway (safechain), rate limits, retries, budgets, fake; DSPy engine
  telemetry/    P6  OpenTelemetry / ee-logging facade (from V1), attribute allowlist, dev OTLP export
  runtime/      P6  durable runner on a Postgres job queue: lanes, leases, submit
  registry/     P4  release manifests: load, pin, resolve; evidence source taxonomy (YAML + workbook)
  intake/       C01 BigQuery case source, normalization, redaction, admission
  understanding/ C02 (DSPy): Pass 1 allegations, requests, agent statements; Pass 2 claims, accounts, evidence needs
  selection/    C03 (DSPy): the customer's main complaint (primary claim), bound to the claim-set hash
  procedures/   Operating procedure PDFs: inspect, extract, redact, snapshot; LightRAG store and procedure.search
  routing/      C04 (DSPy): one vertical per claim from LightRAG sections and the vertical definitions
  console/      the CFR console: a localhost website that runs every command below as a job (LDR-0023)
registry/       release manifests (releases/<id>.yaml), evidence source taxonomy (evidence_sources/<version>.yaml)
config/         ee-config template (config_gpt.example.yml); your config_gpt.yml and config/verticals/ are gitignored
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

## The console

Everything below can also be done from a website on your machine: admit cases, start and stop
the worker, run the trials, snapshots and LightRAG builds, score, route, check the gateway,
migrate and run the tests, then read each job's live log and download its files. It also shows
the pipeline by stage, each case run with its timeline and stage results, and the releases.

```bash
.venv/bin/python -m pip install -e ".[console]" "opentelemetry-api==1.39.1" "opentelemetry-sdk==1.39.1"
.venv/bin/python -m cfr_v2.console          # http://127.0.0.1:8765  (--port to change it)
```

- **Localhost only.** It listens on 127.0.0.1, refuses other host names, and every action
  carries a token from the page, so another website can't start jobs.
- **Settings come from `.env`,** read once at start and passed to every job; restart the
  console after editing it. The System page lists them, secrets hidden.
- **Each action is the terminal command,** run as a job under `var/console/jobs/<id>/`: its
  `log.txt`, `inputs/` (uploads) and `outputs/` (files it wrote). The job page shows the exact
  command line. Jobs run in their own process session, so restarting the console doesn't stop
  a worker or a build; a restarted console picks them up again.
- **Stop** sends SIGTERM (a worker finishes the cases it holds); **Force stop** sends SIGKILL.
- **The worker form needs a cap** on queued runs and shows the queue first, so a worker can't
  start over a backlog by accident.
- **Case pages show INTERNAL stage results only** (claims, the selection, routing decisions).
  RESTRICTED artifacts (transcripts, source records) are never read. Trace and log links open
  Grafana (`CFR_GRAFANA_URL`, default `http://localhost:3000`).
- **Trial review files hold case or procedure text.** They stay in the job's `outputs/`
  (gitignored `var/`); delete the job from its page once reviewed.

## Running intake

```bash
set -a; source .env; set +a
.venv/bin/python -m cfr_v2.intake --synthetic 20 --seed 1                 # offline, synthetic rows
.venv/bin/python -m cfr_v2.intake --start-date 2026-09-01 --end-date 2026-09-07   # BigQuery (enterprise extra)
.venv/bin/python -m cfr_v2.intake --case-nos-from cases.xlsx --missing-out missing.txt   # BigQuery, listed cases
.venv/bin/python -m cfr_v2.intake --print-sql > intake.sql                # the BigQuery job's SQL, e.g. to check its cost
```

- **One way of choosing cases per run:** a resolution-date window, a list of case numbers, or
  synthetic rows.
- **`--case-nos-from`** reads a `case_no` column (`--column` for another header, `--sheet` for
  another worksheet) from an `.xlsx` or `.csv`. It runs the same query as a date window, with the
  date condition replaced by the list (one `ARRAY<STRING>` parameter), so filters, transcript
  matching and the baseline are the same as in production. The summary adds `requested` and
  `not_returned`; the numbers not returned go only to `--missing-out`.
- **`--seed`** picks synthetic case numbers. A case already admitted comes back `DUPLICATE` and
  nothing new is queued, so a second synthetic batch needs a new seed.
- **Release check:** intake refuses (exit 2) a release whose stage versions differ from the
  code's, before it touches anything (LDR-0022). `--allow-incompatible` overrides it.

One BigQuery job returns the cases and each customer's LUMI baseline (approved account facts,
`new_demographics.sql`). The baseline is stored per case: the model-visible profile as INTERNAL,
the account ref map (`rel_id`, `cm15`) as RESTRICTED. A case without a match is still admitted and
counted under `NO_BASELINE` in the summary. `CFR_INTAKE_BASELINE=0` fetches cases only.

Intake is submit-only: it admits and enqueues. Workflows run in worker processes:

```bash
.venv/bin/python -m cfr_v2.runtime                    # serve every lane until SIGTERM / Ctrl+C
.venv/bin/python -m cfr_v2.runtime --lanes realtime   # one lane only
.venv/bin/python -m cfr_v2.runtime --max-queued 10    # refuse to start if more than 10 jobs wait
```

Run as many workers as needed; any worker picks up jobs whose previous worker died.

Workers run C02, C03 and C04 for real (Pass 1, Pass 2, primary selection, then routing: about two
gpt-5 calls per case for C02, one for C03 with two or more claims, and two per claim for C04) and a
placeholder for C05, so **starting a worker sends every queued case to the model.**

- **Before claiming anything, a worker logs the queued and running jobs by release** and refuses
  to start (exit 2) while any belong to a release whose stage versions differ from the code's, or
  that was never pinned, or when more than `--max-queued` are waiting (LDR-0022). Jobs are left
  untouched. `--allow-incompatible` overrides the release check.
- **Check the queue before starting a worker,** and use `--max-queued` when testing: queued runs
  from earlier batches run too.

Try the stages on a few cases first:

```bash
python -m cfr_v2.understanding --sample 5 --out c02_trial   # counts, latency, tokens, cost projection per step
```

Pass 2 tags each evidence need with a source type from `registry/evidence_sources/`, in the
version the run's release manifest names (`evidence_sources:`; `evidence-sources-1` if none). New
intake uses `cfr-v2-phase-e` (`CFR_RELEASE_ID`). The business team maintains where
each source type's evidence lives through a workbook: `python -m cfr_v2.registry.evidence_sources
export|import|check` ([guide](lld/guides/evidence-sources.md); `lld/` is local).

The trial writes nothing to the state database or the artifact store. The review files in `--out` hold
redacted transcript excerpts: keep them local and delete them after review.

To score Pass 1 against the business team's gold-set workbooks (recall, precision, agreement):

```bash
python -m cfr_v2.understanding.score template --cases-file cases.txt gold_template.xlsx   # blank workbook
python -m cfr_v2.understanding.score run gold_*.xlsx --out pass1_score.json               # needs RESTRICTED access
```

## Procedures and routing (C04)

Routing sends each claim to one vertical, or `UNMAPPED` / `NO_ELIGIBLE_VERTICAL`, from the claim,
sections of the business's operating procedures found with LightRAG (`mix` mode), and the vertical
definitions (LDR-0019, LDR-0020, LDR-0021). Prepare once per update, in this order:

```bash
python -m cfr_v2.procedures snapshot /path/to/pdfs                         # extract, redact, store; prints the snapshot ref
python -m cfr_v2.procedures lightrag-build sha256:<snapshot> --workdir lightrag_store   # ~3 gpt-5 calls per section; resumable
python -m cfr_v2.routing publish-verticals config/verticals/knowledge_document.txt      # prints the document's ref
```

- **The release pins both inputs:** `routing_verticals` (the document's ref) and
  `procedure_snapshot` in `registry/releases/<release>.yaml`. A run is routed with what its release
  names, so updating either means a new release.
- **The knowledge document is internal:** keep it in `config/verticals/` (gitignored), never in git.
- **The worker searches the store in `CFR_LIGHTRAG_WORKDIR`.** `lightrag-build` writes
  `cfr_store.json` there, naming the snapshot it was built from, and the worker refuses a store
  whose marker doesn't match the release. A build into a store made from another snapshot is
  refused too.
- **A claim set with no claims is routed without any of this.** Any failed claim fails the stage,
  and the worker retries it. A search the gateway blocks routes that claim without sections.

Outside the workflow:

```bash
python -m cfr_v2.procedures search sha256:<snapshot> "a query" --workdir lightrag_store   # try searches
python -m cfr_v2.routing sha256:<snapshot> --workdir lightrag_store \
  --verticals config/verticals/knowledge_document.txt --claims-from c02_trial --out router_trial   # trial + routing.xlsx
python -m cfr_v2.routing backfill --from-release <old release> --count-only               # route stored claim sets of failed runs
```

Trial, review and backfill files can hold claim or procedure text: keep them local and delete them
after review.

**Changing a stage's version** (any `STAGES` entry): add a release pinning the new version and make
it the default (`IntakeSettings.release_id`, `.env.example`). A unit test fails until you do, and
workers refuse to start while runs of the old release are queued (LDR-0022).

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

Then open Grafana at http://localhost:3000. The **CFR overview** dashboard shows a build's
progress, time per stage, slow traces and logs. Span attributes are allowlisted
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
