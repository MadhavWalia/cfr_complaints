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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cfr_v2.telemetry import get_logger

from . import cases, commands, formats, optimize_view, release_files
from . import status as pipeline
from .diagnosis import diagnose
from .jobs import Job, JobRunner, JobStatus, read_progress
from .settings import ConsoleSettings, display_value

logger = get_logger("cfr_v2.console")
HERE = Path(__file__).parent
DB_CONNECT_TIMEOUT_SEC = 3  # a stopped Postgres must not hang the page
JOURNAL_ROWS = 10
AREA_ROWS = 5  # recent jobs under an area's commands
LOG_TAIL_BYTES = 200_000  # the job page opens on the log's last 200 KB; the full log downloads
LOG_CHUNK = 64_000
LOG_POLL_SEC = 0.5
JOB_CHOICES = 20  # earlier jobs offered as a command's input

# The rail, in order: (path, label). Areas not built yet render their plan.
AREAS = (
    ("/", "Pipeline"),
    ("/jobs", "Jobs"),
    ("/cases", "Cases"),
    ("/releases", "Releases"),
    ("/intake", "Intake"),
    ("/routing", "Routing"),
    ("/procedures", "Procedures"),
    ("/evaluation", "Evaluation"),
    ("/optimize", "Optimize"),
    ("/system", "System"),
    ("/help", "Help"),
)
LEDES = {  # one line under an area's title: what it's for
    "/intake": "Admit a date window, a spreadsheet of case numbers, or synthetic cases; show the query.",
    "/routing": "The router trial and routing.xlsx, publishing the vertical definitions, and the backfill.",
    "/procedures": "Inspect, extract and snapshot procedure PDFs; build and search the LightRAG store.",
    "/evaluation": "The C02 and C03 trial, gold-set templates and scoring, and the evidence-source workbook.",
    "/optimize": "Improve a prompt's instructions with DSPy against a gold set: write the template, check the "
    "filled workbooks, score the baseline, optimize, and review the patch.",
    "/system": "Start and stop the worker, check the gateway, run migrations and tests, and see the settings in use.",
}
ERROR_TITLES = {
    403: "This page has expired",
    404: "Not found",
    409: "Not now",
    422: "Can't use that",
    503: "Can't reach the state database",
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
    app.state.store = None  # set by tests to an in-memory store
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(areas=AREAS, catalogue=commands.BY_ID, tag_help=commands.TAGS)
    templates.env.filters.update(
        when=formats.when, stamp=formats.stamp, took=formats.took, size=formats.size, shell=formats.shell
    )

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context.setdefault("running", sum(1 for j in runner.list() if j.status is JobStatus.RUNNING))
        context.update(
            csrf=app.state.csrf,
            current=request.url.path,
            release_id=settings.release_id,
            grafana_url=settings.grafana_url,
            updated=formats.clock(),
        )
        return templates.TemplateResponse(request, name, context)

    def database():
        if app.state.db is None:
            from cfr_v2.runtime import RuntimeSettings
            from cfr_v2.state import Database

            url = _with_connect_timeout(RuntimeSettings.from_env(settings.env).state_database_url)
            app.state.db = Database(url, max_connections=4, acquire_timeout_sec=5)
        return app.state.db

    def artifact_store():
        """Created when first needed: the S3 settings are read only then."""
        if app.state.store is None:
            from cfr_v2.artifacts import S3ArtifactStore, S3Settings

            app.state.store = S3ArtifactStore(S3Settings.from_env(settings.env))
        return app.state.store

    def resolver(db):
        from cfr_v2.contracts import AuthorizationContext
        from cfr_v2.registry import load_pinned

        reader = AuthorizationContext(principal="console", purpose="queued releases")
        return lambda ref: load_pinned(ref, store=artifact_store(), db=db, authorization=reader)

    def read_status() -> pipeline.PipelineStatus:
        try:
            db = database()
            return pipeline.read_status(db, resolver(db))
        except Exception as exc:  # noqa: BLE001 - the page shows the type; the message can name hosts
            logger.warning("console: state database unavailable: %s", type(exc).__name__)
            return pipeline.unavailable(type(exc).__name__)

    runner = JobRunner(settings.workspace / "jobs", cwd=settings.repo_root, env=settings.env)
    app.state.runner = runner
    templates.env.globals["progress_of"] = lambda job: (
        read_progress(runner.log_path(job.id)) if job.status is JobStatus.RUNNING else None
    )
    form_context = commands.Context(
        root=settings.repo_root,
        env=settings.env,
        done_jobs=lambda command: [j for j in runner.list() if j.command == command and j.status is JobStatus.DONE][
            :JOB_CHOICES
        ],
        outputs=lambda job_id: runner.folder(job_id) / "outputs",
    )

    def console_worker(jobs: list[Job]) -> Job | None:
        return next((j for j in jobs if j.command == "system.worker" and j.status is JobStatus.RUNNING), None)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        jobs = runner.list()
        status, worker = read_status(), console_worker(jobs)
        return page(
            request,
            "pipeline.html",
            status=status,
            attention=pipeline.attention(status, jobs, worker_job=worker),
            view="home",
            jobs=jobs[:JOURNAL_ROWS],
            worker=worker,
        )

    @app.get("/partials/pipeline-status", response_class=HTMLResponse)
    def pipeline_status(request: Request, view: str = "home"):
        """The panel, refreshed: the home page's (what needs attention, the stage line and the
        queue), or System's (the queue only)."""
        status = read_status()
        if view == "queue":
            return page(request, "_pipeline_status.html", status=status, view="queue")
        jobs = runner.list()
        found = pipeline.attention(status, jobs, worker_job=console_worker(jobs))
        return page(request, "_pipeline_status.html", status=status, attention=found, view="home")

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
            diagnosis=diagnose(job.reason),
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
            diagnosis=diagnose(job.reason),
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

    @app.post("/jobs/{job_id}/delete", dependencies=[Depends(require_csrf)])
    def job_delete(job_id: str):
        _job(job_id)
        try:
            runner.delete(job_id)
        except RuntimeError:
            raise HTTPException(status_code=409, detail="Stop the job before deleting it.") from None
        return RedirectResponse("/jobs", status_code=303)

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

    def environment() -> list[tuple[str, str]]:
        """The settings read from .env, secrets hidden, passwords taken out of URLs."""
        return [(key, display_value(key, settings.env.get(key, ""))) for key in settings.env_file_keys]

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """A page for people, JSON for anything else (htmx partials included)."""
        wants_page = "text/html" in request.headers.get("accept", "") and "hx-request" not in request.headers
        if not wants_page:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        response = page(
            request,
            "error.html",
            title=ERROR_TITLES.get(exc.status_code, "Something went wrong"),
            code=exc.status_code,
            detail=exc.detail if isinstance(exc.detail, str) else None,
        )
        response.status_code = exc.status_code
        return response

    # --- cases and releases (decisions 7 and 8) -------------------------------------------

    def _unavailable(exc: Exception) -> str:
        logger.warning("console: state database unavailable: %s", type(exc).__name__)
        return type(exc).__name__

    @app.get("/cases", response_class=HTMLResponse)
    def case_list(
        request: Request, state: str | None = None, release: str | None = None, q: str | None = None, offset: int = 0
    ):
        rows, error = [], None
        try:
            rows = cases.list_cases(database(), state=state, release=release, q=q, offset=offset)
        except Exception as exc:  # noqa: BLE001 - the page says the database is down
            error = _unavailable(exc)
        filters = {
            "state": state if state in cases.STATES else None,
            "release": release or None,
            "q": (q or "").strip(),
        }
        return page(
            request,
            "cases.html",
            title="Cases",
            rows=rows[: cases.PAGE_ROWS],
            more=len(rows) > cases.PAGE_ROWS,
            offset=max(0, offset),
            page_rows=cases.PAGE_ROWS,
            filters=filters,
            states=[(s, cases.STATE_LABELS[s]) for s in cases.STATES],
            releases=[path.stem for path in sorted((settings.repo_root / "registry" / "releases").glob("*.yaml"))],
            error=error,
        )

    def _case(case_run_id: str):
        try:
            run = cases.read_case(database(), case_run_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail=f"Can't read the state database ({_unavailable(exc)})."
            ) from None
        if run is None:
            raise HTTPException(status_code=404, detail="No such case run.")
        return run

    @app.get("/cases/{case_run_id}", response_class=HTMLResponse)
    def case_page(request: Request, case_run_id: str):
        run = _case(case_run_id)
        results: dict[str, Any] = {}  # stage → parsed result, or the reason it isn't shown
        for stage in run.stages:
            if not stage.refs:
                continue
            try:
                parsed = cases.parse_result(cases.read_result(database(), artifact_store(), run, stage.refs[0]))
                results[stage.task] = "" if parsed is None else parsed  # "": not JSON, offered as a download
            except cases.NotShown:
                results[stage.task] = cases.Withheld("It isn't INTERNAL, so the console doesn't show it.")
            except Exception as exc:  # noqa: BLE001 - one unreadable result mustn't hide the case
                logger.warning("console: stage result unreadable: %s", type(exc).__name__)
                results[stage.task] = cases.Withheld(f"Can't read it from the artifact store ({type(exc).__name__}).")
        claims = {}
        pass2 = results.get("c02.pass2")
        if isinstance(pass2, dict):
            claims = {c.get("claim_id"): c for c in pass2.get("claims", []) if isinstance(c, dict)}
        return page(
            request,
            "case.html",
            title=f"Case {run.row.case_no}",
            run=run,
            results=results,
            claims=claims,
            trace_url=cases.trace_url(settings.grafana_url, run),
            logs_url=cases.logs_url(settings.grafana_url, run),
        )

    @app.get("/cases/{case_run_id}/results/{ref}")
    def case_result(case_run_id: str, ref: str):
        run = _case(case_run_id)
        try:
            data = cases.read_result(database(), artifact_store(), run, ref)
        except cases.NotShown:
            raise HTTPException(status_code=404, detail="Not a stage result of this case run.") from None
        stage = next(s.task for s in run.stages if ref in s.refs)
        name = f"{run.row.case_no}-{stage}.json"
        return Response(
            data, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return page(request, "help.html", title="Help")

    @app.get("/releases", response_class=HTMLResponse)
    def releases(request: Request):
        def rows():
            with database().transaction() as tx:
                return tx.all(cases.RELEASES_SQL)

        found, error = cases.read_releases(settings.repo_root / "registry", rows)
        if error:
            logger.warning("console: state database unavailable: %s", error)
        for release in found:
            release.blockers = cases.blockers(release, settings.repo_root, settings.env)
        query = request.query_params
        values = {f.name: v for f in commands.NEW_RELEASE if (v := f.initial(form_context)) is not None}
        if query.get("snapshot"):  # from a finished build: "make a release with this snapshot"
            values["procedure_snapshot"] = query["snapshot"]
        by_id = {r.release_id: r for r in found}
        written = by_id.get(query.get("written", ""))
        base = by_id.get(query.get("base", ""))
        return releases_page(request, found, error, values=values, written=written, base=base)

    def releases_page(request: Request, found, error, *, values, form_error=None, written=None, base=None):
        changed = []
        if written and written.manifest and base and base.manifest:
            changed = [
                (key, getattr(base.manifest, key), getattr(written.manifest, key))
                for key in release_files.PINS
                if getattr(base.manifest, key) != getattr(written.manifest, key)
            ]
        return page(
            request,
            "releases.html",
            title="Releases",
            releases=found,
            error=error,
            new_release=commands.NEW_RELEASE,
            values=values,
            form_error=form_error,
            written=written,
            base=base,
            changed=changed,
            ctx=form_context,
        )

    @app.post("/releases/new", dependencies=[Depends(require_csrf)])
    async def new_release(request: Request):
        """Writes registry/releases/<id>.yaml from another release, with new routing pins."""
        form = await request.form()
        raw = {k: v for k, v in form.multi_items() if isinstance(v, str) and k != "csrf"}
        try:
            values = {f.name: f.parse(f.read(form), form_context) for f in commands.NEW_RELEASE}
            path = release_files.write_release(
                settings.repo_root / "registry",
                values["base"],
                values["release_id"],
                {key: values[key] for key in release_files.PINS},
            )
        except (commands.FormError, release_files.ReleaseFileError) as exc:
            found, _ = cases.read_releases(settings.repo_root / "registry", None)
            for release in found:
                release.blockers = cases.blockers(release, settings.repo_root, settings.env)
            raw = {
                f.name: (raw.get(f"{f.name}_other") if raw.get(f.name) == commands.OTHER else raw.get(f.name))
                for f in commands.NEW_RELEASE
            }
            response = releases_page(request, found, None, values=raw, form_error=str(exc))
            response.status_code = 422
            return response
        logger.info("console: wrote release %s from %s", path.stem, values["base"])
        return RedirectResponse(f"/releases?written={path.stem}&base={values['base']}#{path.stem}", status_code=303)

    @app.post("/releases/{release_id}/current", dependencies=[Depends(require_csrf)])
    def make_current(release_id: str):
        """Sets CFR_RELEASE_ID in .env: the release intake admits new cases under."""
        found, _ = cases.read_releases(settings.repo_root / "registry", None)
        release = next((r for r in found if r.release_id == release_id), None)
        if release is None:
            raise HTTPException(status_code=404, detail="No such release in the registry.")
        problems = cases.blockers(release, settings.repo_root, settings.env)
        if problems:
            raise HTTPException(status_code=409, detail=f"{release_id} can't be made current: {'; '.join(problems)}.")
        previous = settings.release_id
        settings.set_in_env_file("CFR_RELEASE_ID", release_id)
        logger.info("console: current release %s -> %s", previous, release_id)
        return RedirectResponse(f"/releases#{release_id}", status_code=303)

    # --- areas and their commands --------------------------------------------------------

    def area_page(
        request: Request,
        area: str,
        label: str,
        *,
        error: str | None = None,
        filled: str | None = None,
        values: dict | None = None,
        again: Job | None = None,
        source: Job | None = None,
        status_code: int = 200,
    ):
        """An area's commands. ``filled`` names the command whose form shows ``values`` (a form sent
        back with ``error``, or an earlier job's form for "Run again", ``again``)."""
        found = commands.for_area(area)
        extra = optimize_page(request) if area == "optimize" else {}
        response = page(
            request,
            "area.html",
            title=label,
            area=area,
            commands=found,
            plan=LEDES["/" + area],
            error=error,
            filled=filled,
            values=values or {},
            again=again,
            source=source,
            ctx=form_context,
            recent=listed(area=area),
            status=read_status() if area == "system" else None,  # the queue, before starting a worker
            view="queue",
            environment=environment() if area == "system" else None,
            **extra,
        )
        response.status_code = status_code
        return response

    def optimize_page(request: Request) -> dict[str, Any]:
        """The prompts, the runs and one run's results; the chosen prompt fills every form."""
        rows = optimize_view.overview(runner)
        by_id = {r.prompt.prompt_id: r for r in rows}
        query = request.query_params
        chosen = by_id.get(query.get("prompt", ""))
        runs = [run for r in rows for run in r.runs]
        runs.sort(key=lambda run: run.job.created_at, reverse=True)
        if chosen is not None:
            runs = [run for run in runs if run.prompt_id == chosen.prompt.prompt_id]
        shown = next((run for run in runs if run.job.id == query.get("run")), runs[0] if runs else None)
        defaults = {}
        if chosen is not None:
            defaults["prompt"] = chosen.prompt.prompt_id
            if chosen.gold_sets:
                defaults["gold"] = chosen.gold_sets[0].id
        diff = []
        if shown is not None and shown.report is not None and shown.report.instructions_after:
            diff = optimize_view.instruction_diff(shown.report.instructions_before, shown.report.instructions_after)
        return {
            "prompt_rows": rows,
            "chosen": chosen,
            "runs": runs,
            "shown": shown,
            "shown_files": runner.files(shown.job.id, "outputs") if shown else [],
            "diff": diff,
            "defaults": defaults,
        }

    for path, label in AREAS[1:]:
        if path in ("/jobs", "/cases", "/releases", "/help"):
            continue

        def show(
            request: Request,
            again: str | None = None,
            fill: str | None = None,
            area: str = path[1:],
            label: str = label,
        ):
            if fill is not None:  # a next step: the command's form, filled in from the query
                command = commands.BY_ID.get(fill)
                if command is None or command.area != area:
                    raise HTTPException(status_code=404, detail="No such command here.")
                query = request.query_params
                values = {f.name: str(v) for f in command.fields if (v := f.initial(form_context)) is not None}
                values |= {f.name: query[f.name] for f in command.fields if f.name in query}
                try:
                    source = runner.get(query.get("from", ""))
                except KeyError:
                    source = None
                return area_page(request, area, label, filled=fill, values=values, source=source)
            if again is None:
                return area_page(request, area, label)
            job = _job(again)
            command = commands.BY_ID.get(job.command)
            if command is None or command.area != area or job.form is None:
                raise HTTPException(status_code=404, detail="That job can't be run again from here.")
            return area_page(request, area, label, filled=command.id, values=job.form, again=job)

        app.add_api_route(path, show, methods=["GET"], response_class=HTMLResponse, name=label.lower())

    @app.post("/commands/{command_id}", dependencies=[Depends(require_csrf)])
    async def run_command(request: Request, command_id: str):
        command = commands.BY_ID.get(command_id)
        if command is None:
            raise HTTPException(status_code=404, detail="No such command.")
        form = await request.form()
        label = dict(AREAS)["/" + command.area]
        try:
            values = command.parse(form, form_context)
        except commands.FormError as exc:
            raw = {k: v for k, v in form.multi_items() if isinstance(v, str) and k != "csrf"}
            return area_page(
                request, command.area, label, error=str(exc), filled=command.id, values=raw, status_code=422
            )

        def argv(folder: Path) -> list[str]:
            for f in command.fields:  # uploads go into the job's inputs/ first
                if f.kind == "file" and values.get(f.name) is not None:
                    values[f.name] = commands.save_upload(values[f.name], folder)
                elif f.kind == "files":
                    values[f.name] = [commands.save_upload(u, folder) for u in values[f.name]]
            return command.argv(values, folder)

        sent = {k: v for k, v in form.multi_items() if isinstance(v, str) and k != "csrf"}
        job = runner.start(command.id, command.describe(values), argv, summarize=command.summarize, form=sent)
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
