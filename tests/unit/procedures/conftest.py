"""LightRAG in tests: no downloads, and a clean slate between tests."""

from __future__ import annotations

import importlib.util
import sys

import pytest


class ByteTokens:
    """Stands in for tiktoken, which downloads its encodings on first use."""

    def encode(self, content):
        return list(content.encode())

    def decode(self, tokens):
        return bytes(tokens).decode(errors="replace")


@pytest.fixture(autouse=True)
def fresh_lightrag_state(monkeypatch):
    """No downloads, and a clean slate: LightRAG keeps its stores in process-wide state."""
    if importlib.util.find_spec("lightrag"):
        from lightrag.utils import Tokenizer

        monkeypatch.setattr(
            "lightrag.lightrag.TiktokenTokenizer", lambda *_: Tokenizer(model_name="bytes", tokenizer=ByteTokens())
        )
    yield
    if "lightrag.kg.shared_storage" in sys.modules:
        sys.modules["lightrag.kg.shared_storage"].finalize_share_data()
