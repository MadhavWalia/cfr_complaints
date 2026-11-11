"""The CFR console (LDR-0023): a local website over the pipeline's own commands.

Bound to 127.0.0.1. Requests for any other host are refused (DNS rebinding), and every request
that changes anything carries the process's CSRF token, because any website the user visits can
send requests to localhost.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cfr_v2.telemetry import get_logger

from . import status as pipeline
from .settings import ConsoleSettings

logger = get_logger("cfr_v2.console")
HERE = Path(__file__).parent
DB_CONNECT_TIMEOUT_SEC = 3  # a stopped Postgres must not hang the page

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

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return page(request, "pipeline.html", status=read_status(), jobs=[])

    @app.get("/partials/pipeline-status", response_class=HTMLResponse)
    def pipeline_status(request: Request):
        return page(request, "_pipeline_status.html", status=read_status())

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    for path, label in AREAS[1:]:

        def planned(request: Request, path: str = path, label: str = label):
            return page(request, "planned.html", title=label, plan=PLANNED[path])

        app.add_api_route(path, planned, methods=["GET"], response_class=HTMLResponse, name=label.lower())

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
