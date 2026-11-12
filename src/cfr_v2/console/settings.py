"""Where the console works, and the environment its jobs inherit (LDR-0023 decisions 4 and 5)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT = 8765
# Shown on the System page without values: anything that looks like a secret.
_SECRET = re.compile(r"SECRET|PASSWORD|TOKEN|KEY|CREDENTIAL|SERVICE_ACCOUNT", re.IGNORECASE)
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines as ``set -a; source .env`` would set them: comments and blank lines
    skipped, an ``export`` prefix allowed, one level of matching quotes removed."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:  # an unquoted trailing comment
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def write_env_value(path: Path, key: str, value: str) -> None:
    """Sets one ``KEY=VALUE`` line in an env file, leaving every other line as it was: the last
    active line for the key is replaced (keeping an ``export`` prefix), else one is added. The file
    is replaced in one step, so a reader never sees half of it."""
    if not re.fullmatch(r"[A-Za-z0-9._:/@-]+", value):
        raise ValueError(f"{key}: not a plain value")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    at = next(
        (i for i in reversed(range(len(lines))) if (m := _LINE.match(lines[i])) and m.group(1) == key),
        None,
    )
    if at is None:
        lines.append(f"{key}={value}")
    else:
        prefix = "export " if lines[at].lstrip().startswith("export ") else ""
        lines[at] = f"{prefix}{key}={value}"
    temporary = path.with_name(path.name + ".console")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if path.is_file():
        temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


def is_secret(key: str) -> bool:
    return bool(_SECRET.search(key))


def display_value(key: str, value: str) -> str:
    """A setting as the console shows it: secrets hidden, a URL's password replaced."""
    if is_secret(key):
        return "set, hidden" if value else "empty"
    parts = urlsplit(value)
    if parts.scheme and parts.password:
        host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))
    return value


@dataclass(frozen=True)
class ConsoleSettings:
    repo_root: Path
    workspace: Path
    env: Mapping[str, str] = field(repr=False)  # what every job runs with: the process's, then .env's
    env_file_keys: tuple[str, ...] = ()
    port: int = DEFAULT_PORT

    @classmethod
    def load(cls, repo_root: Path, *, workspace: Path | None = None, port: int = DEFAULT_PORT) -> ConsoleSettings:
        from_file = read_env_file(repo_root / ".env")
        env = {**os.environ, **from_file}  # as `set -a; source .env`: the file's values win
        return cls(
            repo_root=repo_root,
            workspace=workspace or repo_root / "var" / "console",
            env=env,
            env_file_keys=tuple(sorted(from_file)),
            port=port,
        )

    def set_in_env_file(self, key: str, value: str) -> None:
        """Writes ``key`` to .env and to the environment jobs start with, so the next job sees it
        without a restart. The only setting the console changes is the current release."""
        write_env_value(self.repo_root / ".env", key, value)
        assert isinstance(self.env, MutableMapping)  # shared with the job runner and the forms
        self.env[key] = value
        if key not in self.env_file_keys:
            object.__setattr__(self, "env_file_keys", tuple(sorted((*self.env_file_keys, key))))

    @property
    def grafana_url(self) -> str:
        return self.env.get("CFR_GRAFANA_URL", "http://localhost:3000").rstrip("/")

    @property
    def release_id(self) -> str:
        from cfr_v2.intake.settings import IntakeSettings

        return IntakeSettings.from_env(self.env).release_id
