"""The CFR console (LDR-0023): a local website over the pipeline's own commands.

Bound to 127.0.0.1. Requests for any other host are refused (DNS rebinding), and every request
that changes anything carries the process's CSRF token, because any website the user visits can
send requests to localhost.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import secrets
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cfr_v2.telemetry import get_logger

from . import commands, formats
from . import status as pipeline
from .jobs import Job, JobRunner, JobStatus
from .settings import ConsoleSettings

logger = get_logger("cfr_v2.console")
HERE = Path(__file__).parent
DB_CONNECT_TIMEOUT_SEC = 3  # a stopped Postgres must not hang the page
JOURNAL_ROWS = 10
AREA_ROWS = 5  # recent jobs under an area's commands
LOG_TAIL_BYTES = 200_000  # the job page opens on the log's last 200 KB; the full log downloads
LOG_CHUNK = 64_000
LOG_POLL_SEC = 0.5

# The rail, in order: (path, label). Areas not built yet render their plan.
AREAS = (
    ("/", "Pipeline"),
    ("/jobs", "Jobs"),
    ("/cases", "Cases"),
    ("/intake", "Intake"),
    ("/routing", "Routing"),
    ("/procedures", "Procedures"),
    ("/evaluation", "Evaluation"),
    ("/system", "System"),
)
PLANNED = {
    "/jobs": "Every job started here: its live log, status and outputs to download, and a way to stop it.",
    "/cases": "Case runs by state and release, and each case's timeline and stage results, with links to its trace.",
    "/intake": "Admit a date window, a spreadsheet of case numbers, or synthetic cases; show the query.",
    "/routing": "The router trial and routing.xlsx, publishing the vertical definitions, and the backfill.",
    "/procedures": "Inspect, extract and snapshot procedure PDFs; build and search the LightRAG store.",
    "/evaluation": "The C02 and C03 trial, gold-set templates and scoring, and the evidence-source workbook.",
    "/system": "Start and stop the worker, releases and their pins, gateway checks, migrations, tests, environment.",
}


def _with_connect_timeout(url: str) -> str:
    if "connect_timeout=" in url:
        return url
    return f"{url}{'&' if urlsplit(url).query else '?'}connect_timeout={DB_CONNECT_TIMEOUT_SEC}"


def _log_tail(path: Path) -> tuple[str, int]:
    """The end of a log, starting at a line, and the offset streaming continues from."""
    if not path.is_file():
        return "", 0
    size = path.stat().st_size
    with path.open("rb") as log:
        log.seek(max(0, size - LOG_TAIL_BYTES))
        data = log.read()
    if size > LOG_TAIL_BYTES:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace"), size


def create_app(settings: ConsoleSettings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        db = app.state.db
        if db is not None:
            db.close()

    app = FastAPI(title="CFR console", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.settings = settings
    app.state.csrf = secrets.token_urlsafe(32)
    app.state.db = None
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(areas=AREAS)
    templates.env.filters.update(when=formats.when, took=formats.took, size=formats.size, shell=formats.shell)

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.update(
            csrf=app.state.csrf,
            current=request.url.path,
            release_id=settings.release_id,
            grafana_url=settings.grafana_url,
        )
        return templates.TemplateResponse(request, name, context)

    def database():
        if app.state.db is None:
            from cfr_v2.runtime import RuntimeSettings
            from cfr_v2.state import Database

            url = _with_connect_timeout(RuntimeSettings.from_env(settings.env).state_database_url)
            app.state.db = Database(url, max_connections=4, acquire_timeout_sec=5)
        return app.state.db

    def resolver(db):
        from cfr_v2.artifacts import S3ArtifactStore, S3Settings
        from cfr_v2.contracts import AuthorizationContext
        from cfr_v2.registry import load_pinned

        reader = AuthorizationContext(principal="console", purpose="queued releases")
        stores: list = []  # the artifact store is needed only when something is queued

        def resolve(ref: str):
            if not stores:
                stores.append(S3ArtifactStore(S3Settings.from_env(settings.env)))
            return load_pinned(ref, store=stores[0], db=db, authorization=reader)

        return resolve

    def read_status() -> pipeline.PipelineStatus:
        try:
            db = database()
            return pipeline.read_status(db, resolver(db))
        except Exception as exc:  # noqa: BLE001 - the page shows the type; the message can name hosts
            logger.warning("console: state database unavailable: %s", type(exc).__name__)
            return pipeline.unavailable(type(exc).__name__)

    runner = JobRunner(settings.workspace / "jobs", cwd=settings.repo_root, env=settings.env)
    app.state.runner = runner

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return page(request, "pipeline.html", status=read_status(), jobs=runner.list(JOURNAL_ROWS))

    @app.get("/partials/pipeline-status", response_class=HTMLResponse)
    def pipeline_status(request: Request):
        return page(request, "_pipeline_status.html", status=read_status())

    def listed(*, area: str | None = None, status: str | None = None, everything: bool = False) -> list[Job]:
        jobs = runner.list(None if everything or area else JOURNAL_ROWS)
        if area:
            jobs = [j for j in jobs if j.command.split(".")[0] == area][:AREA_ROWS]
        if status in {s.value for s in JobStatus}:
            jobs = [j for j in jobs if j.status.value == status]
        return jobs

    @app.get("/partials/journal", response_class=HTMLResponse)
    def journal(request: Request, area: str | None = None, status: str | None = None, all: bool = False):
        url = str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")
        return page(request, "_journal.html", jobs=listed(area=area, status=status, everything=all), journal_url=url)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # --- jobs ----------------------------------------------------------------------------

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, status: str | None = None):
        valid = status if status in {s.value for s in JobStatus} else None
        return page(
            request,
            "jobs.html",
            title="Jobs",
            jobs=listed(status=valid, everything=True),
            filter=valid,
            statuses=list(JobStatus),
        )

    def _job(job_id: str) -> Job:
        try:
            return runner.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="No such job.") from None

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = _job(job_id)
        text, offset = _log_tail(runner.log_path(job_id))
        return page(
            request,
            "job.html",
            title=job.title,
            job=job,
            log=text,
            offset=offset,
            outputs=runner.files(job_id, "outputs"),
            inputs=runner.files(job_id, "inputs"),
            command=commands.BY_ID.get(job.command),
        )

    @app.get("/jobs/{job_id}/head", response_class=HTMLResponse)
    def job_head(request: Request, job_id: str):
        job = _job(job_id)
        return page(
            request,
            "_job_head.html",
            job=job,
            outputs=runner.files(job_id, "outputs"),
            command=commands.BY_ID.get(job.command),
        )

    @app.get("/jobs/{job_id}/log", response_class=EventSourceResponse)
    async def job_log(request: Request, job_id: str, offset: int = 0) -> AsyncIterable[ServerSentEvent]:
        """The log from ``offset`` on, as it grows; ``end`` once the job has finished."""
        _job(job_id)
        position = int(request.headers.get("last-event-id") or offset)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        path = runner.log_path(job_id)
        while not await request.is_disconnected():
            with path.open("rb") as log:
                log.seek(position)
                chunk = log.read(LOG_CHUNK)
            if chunk:
                position += len(chunk)
                yield ServerSentEvent(data={"text": decoder.decode(chunk)}, event="log", id=str(position))
                continue
            job = runner.get(job_id)
            if job.finished:
                yield ServerSentEvent(data={"status": job.status.value}, event="end")
                return
            await asyncio.sleep(LOG_POLL_SEC)

    @app.post("/jobs/{job_id}/stop", dependencies=[Depends(require_csrf)])
    async def job_stop(request: Request, job_id: str):
        form = await request.form()
        _job(job_id)
        runner.stop(job_id, force=form.get("force") == "1")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}/log.txt")
    def job_log_file(job_id: str):
        _job(job_id)
        return FileResponse(runner.log_path(job_id), filename=f"{job_id}.log", media_type="text/plain")

    @app.get("/jobs/{job_id}/files/{kind}/{relative:path}")
    def job_file(job_id: str, kind: str, relative: str):
        try:
            path = runner.file(job_id, kind, relative)
        except KeyError:
            raise HTTPException(status_code=404, detail="No such file.") from None
        return FileResponse(path, filename=path.name)

    # --- areas and their commands --------------------------------------------------------

    def area_page(
        request: Request,
        area: str,
        label: str,
        *,
        error: str | None = None,
        failed: str | None = None,
        values: dict | None = None,
        status_code: int = 200,
    ):
        found = commands.for_area(area)
        if not found:
            return page(request, "planned.html", title=label, plan=PLANNED["/" + area])
        response = page(
            request,
            "area.html",
            title=label,
            area=area,
            commands=found,
            plan=PLANNED["/" + area],
            error=error,
            failed=failed,
            values=values or {},
            recent=listed(area=area),
        )
        response.status_code = status_code
        return response

    for path, label in AREAS[1:]:
        if path in ("/jobs",):
            continue

        def show(request: Request, area: str = path[1:], label: str = label):
            return area_page(request, area, label)

        app.add_api_route(path, show, methods=["GET"], response_class=HTMLResponse, name=label.lower())

    @app.post("/commands/{command_id}", dependencies=[Depends(require_csrf)])
    async def run_command(request: Request, command_id: str):
        command = commands.BY_ID.get(command_id)
        if command is None:
            raise HTTPException(status_code=404, detail="No such command.")
        form = await request.form()
        label = dict(AREAS)["/" + command.area]
        try:
            values = command.parse(form)
        except commands.FormError as exc:
            raw = {k: v for k, v in form.items() if isinstance(v, str) and k != "csrf"}
            return area_page(
                request, command.area, label, error=str(exc), failed=command.id, values=raw, status_code=422
            )

        def argv(folder: Path) -> list[str]:
            for f in command.fields:  # uploads go into the job's inputs/ first
                if f.kind == "file" and values.get(f.name) is not None:
                    values[f.name] = commands.save_upload(values[f.name], folder)
            return command.argv(values, folder)

        job = runner.start(command.id, command.describe(values), argv, summarize=command.summarize)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    return app


async def require_csrf(request: Request) -> None:
    """For every request that changes anything: the token from the page, in the ``X-CSRF-Token``
    header (htmx) or a ``csrf`` form field (plain forms)."""
    expected = request.app.state.csrf
    sent = request.headers.get("x-csrf-token")
    if sent is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        sent = (await request.form()).get("csrf")
    if not isinstance(sent, str) or not secrets.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="Missing or wrong CSRF token: reload the page and try again.")


logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # a line per poll would bury job logs
