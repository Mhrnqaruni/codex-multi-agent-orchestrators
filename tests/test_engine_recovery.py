"""Exercise canonical workflow recovery, not source-text patch markers."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import congress2 as congress


@pytest.fixture
def engine(tmp_path):
    app = congress.Congress.__new__(congress.Congress)
    app._interrupt_requested = False
    app.researcher_session_id = None
    app.codex_bin = "fictional-codex"
    app.working_dir = str(tmp_path)
    app.ui = SimpleNamespace(status=Mock(), error=Mock())
    app.logger = SimpleNamespace(log_master=Mock())
    app._wait_for_rate_limit = Mock(return_value="auto_retry")
    app._wait_for_internet = Mock(return_value=True)
    app._save_retry_state = Mock()
    app._active_user_query = "fictional request"
    app.output_files = []
    return app


def test_rate_limit_retries_are_bounded(engine, monkeypatch):
    run = Mock(return_value=("", "rate limit exceeded", 1, 0.01, "fictional-thread"))
    monkeypatch.setattr(congress, "run_codex", run)
    result = engine._run_with_recovery("researcher", "task", 1, threading.Event())
    assert result[2] == -2
    assert "budget exhausted" in result[1]
    assert run.call_count == congress.MAX_RECOVERY_RETRIES + 1


@pytest.mark.parametrize("error", ["billing limit", "unauthorized", "policy denied", "invalid request"])
def test_permanent_failure_stops_immediately(engine, monkeypatch, error):
    run = Mock(return_value=("partial", error, 1, 0.01, None))
    monkeypatch.setattr(congress, "run_codex", run)
    result = engine._run_with_recovery("researcher", "task", 1, threading.Event())
    assert result[0] == ""
    assert result[2] != 0
    assert run.call_count == 1
    engine._wait_for_rate_limit.assert_not_called()


def test_network_failure_saves_state_and_stops(engine, monkeypatch):
    run = Mock(return_value=("", "network unavailable", -3, 0.01, None))
    monkeypatch.setattr(congress, "run_codex", run)
    result = engine._run_with_recovery("researcher", "task", 1, threading.Event())
    assert result[2] == -2
    assert run.call_count == congress.MAX_RECOVERY_RETRIES + 1
    assert engine._save_retry_state.call_count == run.call_count


def test_success_preserves_session(engine, monkeypatch):
    monkeypatch.setattr(
        congress, "run_codex", Mock(return_value=("deliverable", "", 0, 1, "fictional-thread"))
    )
    result = engine._run_with_recovery("researcher", "task", 1, threading.Event())
    assert result[:3] == ("deliverable", "", 0)
    assert engine.researcher_session_id == "fictional-thread"


def test_legacy_import_is_canonical():
    import congress as legacy

    assert legacy.Congress is congress.Congress
