"""Opt-in structured exploration tracing (Phase 12): GEMMAQA_EXPLORATION_TRACE=true
records per-iteration decisions (candidates, goals, gaps, progress, stop policy) for
debugging — disabled by default, and must never leak credentials/passwords/field
values even when enabled."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.utils import exploration_trace  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GEMMAQA_EXPLORATION_TRACE", raising=False)
    yield


def test_disabled_by_default():
    assert exploration_trace.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_enabled_by_truthy_values(monkeypatch, value):
    monkeypatch.setenv("GEMMAQA_EXPLORATION_TRACE", value)
    assert exploration_trace.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_disabled_by_falsy_values(monkeypatch, value):
    monkeypatch.setenv("GEMMAQA_EXPLORATION_TRACE", value)
    assert exploration_trace.enabled() is False


def test_record_is_a_no_op_when_disabled():
    with patch.object(exploration_trace, "logger") as mock_logger:
        exploration_trace.record("iteration.plan", page_url="https://example.com")
        mock_logger.info.assert_not_called()


def test_record_logs_when_enabled(monkeypatch):
    monkeypatch.setenv("GEMMAQA_EXPLORATION_TRACE", "true")
    with patch.object(exploration_trace, "logger") as mock_logger:
        exploration_trace.record("iteration.plan", page_url="https://example.com")
        mock_logger.info.assert_called_once()


def test_password_and_value_fields_redacted_at_any_depth(monkeypatch):
    monkeypatch.setenv("GEMMAQA_EXPLORATION_TRACE", "true")
    with patch.object(exploration_trace, "logger") as mock_logger:
        exploration_trace.record(
            "iteration.result",
            action="fill",
            value="Secret123!",
            nested={"password": "hunter2", "username": "alice@example.com", "safe_key": "kept"},
            candidates=[{"candidate_id": "c1", "current_value": "top-secret"}],
        )
        (call_args,) = mock_logger.info.call_args_list
        logged = call_args.args[2]  # ("EXPLORATION_TRACE %s %s", event, sanitized_fields)
        assert logged["value"] == "***redacted***"
        assert logged["nested"]["password"] == "***redacted***"
        assert logged["nested"]["username"] == "***redacted***"
        assert logged["nested"]["safe_key"] == "kept"
        assert logged["candidates"][0]["current_value"] == "***redacted***"
        assert logged["candidates"][0]["candidate_id"] == "c1"


def test_record_never_raises_even_with_unusual_payloads(monkeypatch):
    monkeypatch.setenv("GEMMAQA_EXPLORATION_TRACE", "true")
    exploration_trace.record("iteration.plan", weird=object(), nested=[1, {"password": "x"}, None])
