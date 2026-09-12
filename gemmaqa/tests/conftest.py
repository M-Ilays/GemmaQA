"""Isolate the UI provider override file so tests never touch the operator's choice."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def _isolate_runtime_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMAQA_RUNTIME_PROVIDER_FILE", str(tmp_path / "runtime-provider"))
    from app.config import get_settings, reset_runtime_provider_state
    from app.gemma import reset_gemma_provider

    reset_runtime_provider_state()
    reset_gemma_provider()
    get_settings.cache_clear()
    yield
    reset_runtime_provider_state()
    reset_gemma_provider()
    get_settings.cache_clear()
