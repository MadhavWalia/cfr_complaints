from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cfr_v2.registry import DEFAULT_REGISTRY_DIR


@dataclass(frozen=True)
class IntakeSettings:
    release_id: str = "cfr-v2-phase-b"
    registry_dir: Path = DEFAULT_REGISTRY_DIR
    # V1 toxic_words.txt; without it profanity is not masked (as in V1).
    toxic_words_file: Path | None = None
    max_workers: int = 8

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "IntakeSettings":
        toxic = env.get("CFR_TOXIC_WORDS_FILE")
        return cls(
            release_id=env.get("CFR_RELEASE_ID", cls.release_id),
            registry_dir=Path(env.get("CFR_REGISTRY_DIR", str(cls.registry_dir))),
            toxic_words_file=Path(toxic) if toxic else None,
            max_workers=int(env.get("CFR_INTAKE_MAX_WORKERS", cls.max_workers)),
        )
