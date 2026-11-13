"""Why a job failed, in a line, and what usually fixes it (LDR-0025 step 1).

``failure_reason`` reads the end of a job's log for the line that says what went wrong: the last
exception of a traceback, an ``error:`` line, a logged ``ERROR``, or pytest's summary. ``diagnose``
matches that line against the failures seen so far and gives a plain sentence and one action.
Logs carry ids, counts and messages only, so the reason does too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TAIL_BYTES = 64_000
MAX_REASON = 400

_EXCEPTION = re.compile(
    r"^(?:[A-Za-z_][\w]*\.)*([A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Refused)\w*)(?::\s*(.*))?$"
)
_ERROR_LINE = re.compile(r"^(?:.*?: )?error: (.+)$")  # argparse and the CLIs' own "error: …"
_LOGGED = re.compile(r"\bERROR\s+\S+ - (.+)$")  # the console's log format: time, level, logger - message
_PYTEST = re.compile(r"^=+ (.*\b\d+ (?:failed|errors?)\b.*?) =+$")


def failure_reason(log: Path) -> str | None:
    """The last line of the log's end that says what failed, shortened; None when none does."""
    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, 2) - TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if match := _EXCEPTION.match(line):
            name, message = match.groups()
            return _short(f"{name}: {message}" if message else name)
        for pattern in (_ERROR_LINE, _LOGGED, _PYTEST):
            if match := pattern.search(line):
                return _short(match.group(1))
    return None


def _short(text: str) -> str:
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 1] + "…"


@dataclass(frozen=True)
class Diagnosis:
    explanation: str  # what happened, in plain words
    action: str | None = None  # the one thing to do
    href: str | None = None  # where to do it


# (pattern over the reason, diagnosis), first match wins. Add a row when a new failure recurs.
KNOWN: tuple[tuple[re.Pattern[str], Diagnosis], ...] = (
    (
        re.compile(r"StoreMismatchError.*was built from"),
        Diagnosis(
            "The LightRAG store already holds another snapshot, and a store holds only one. Build this "
            "snapshot into its own folder: leave the store empty.",
            "Build the store",
            "/procedures#procedures.build",
        ),
    ),
    (
        re.compile(r"no LightRAG store at|has no cfr_store\.json"),
        Diagnosis(
            "No LightRAG store has been built for this snapshot here.",
            "Build the store",
            "/procedures#procedures.build",
        ),
    ),
    (
        re.compile(r"OperationalError|Connection refused|could not connect|PoolTimeout", re.IGNORECASE),
        Diagnosis(
            "The state database couldn't be reached. Start Postgres with docker compose up -d, then run it again."
        ),
    ),
    (
        re.compile(r"UndefinedTable|UndefinedColumn|migration", re.IGNORECASE),
        Diagnosis("The state database is behind the code.", "Run the migrations", "/system#system.migrate"),
    ),
    (
        re.compile(r"not starting|IncompatibleReleaseError|doesn't match this code|more than \d+ .*queued"),
        Diagnosis(
            "The worker refused to start: the queue holds runs it shouldn't take, or more than the cap.",
            "See the queue",
            "/system#queue-h",
        ),
    ),
    (
        re.compile(r"pins no routing_verticals|ManifestError|no manifest for release"),
        Diagnosis("The release can't be used as it is.", "See the releases", "/releases"),
    ),
    (
        re.compile(r"\b(?:401|403)\b|Unauthorized|Forbidden|AuthenticationError", re.IGNORECASE),
        Diagnosis(
            "The model gateway refused the credentials. Check the gateway settings in .env.",
            "Check the gateway",
            "/system#system.smoke",
        ),
    ),
    (
        re.compile(r"\b\d+ failed\b|\b\d+ errors?\b"),
        Diagnosis("Some tests failed: the log names each one, above its summary."),
    ),
)


def diagnose(reason: str | None) -> Diagnosis | None:
    if not reason:
        return None
    return next((d for pattern, d in KNOWN if pattern.search(reason)), None)
