"""Where the console works, and the environment its jobs inherit (LDR-0023 decisions 4 and 5)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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


def is_secret(key: str) -> bool:
    return bool(_SECRET.search(key))


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

    @property
    def grafana_url(self) -> str:
        return self.env.get("CFR_GRAFANA_URL", "http://localhost:3000").rstrip("/")

    @property
    def release_id(self) -> str:
        from cfr_v2.intake.settings import IntakeSettings

        return IntakeSettings.from_env(self.env).release_id
