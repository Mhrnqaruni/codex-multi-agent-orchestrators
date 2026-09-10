"""Offline entry points, retention, and isolation behavior."""

import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from codex_orchestrators.cli import main, purge_diagnostics
from codex_orchestrators.metadata import MetadataLogger
from codex_orchestrators.policies import PolicyError
from codex_orchestrators.workspace import prepare_worktree


def test_demo_is_offline(capsys, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("demo spawned a process"))
    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["live_model_calls"] == 0
    assert all(result["checks"].values())


def test_policy_does_not_spawn(capsys, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("policy spawned a process"))
    assert main(["policy", "--role", "review"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["role"] == "review"


def test_purge_previews_then_removes_only_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_orchestrators.metadata.state_base", lambda: tmp_path)
    monkeypatch.setattr("codex_orchestrators.cli.state_base", lambda: tmp_path)
    logger = MetadataLogger()
    artifact = Path(logger.session_dir) / "events.jsonl"
    old = time.time() - 40 * 86400
    os.utime(artifact, (old, old))
    unrelated = tmp_path / "private-state.json"
    unrelated.write_text("fictional")
    assert purge_diagnostics(older_than_days=30) == 1
    assert artifact.exists()
    assert purge_diagnostics(older_than_days=30, apply=True) == 1
    assert not artifact.exists()
    assert unrelated.exists()


def test_clean_worktree_isolated_and_dirty_source_refused(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Synthetic Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (source / "example.txt").write_text("original")
    git("add", "example.txt")
    git("-c", "commit.gpgsign=false", "-c", "core.hooksPath=", "commit", "-m", "Synthetic fixture")
    destination = tmp_path / "isolated"
    base = prepare_worktree(source, destination, "orchestrators/fictional-run")
    assert base == git("rev-parse", "HEAD")
    (destination / "example.txt").write_text("changed")
    assert (source / "example.txt").read_text() == "original"
    (source / "untracked.txt").write_text("user work")
    with pytest.raises(PolicyError, match="dirty"):
        prepare_worktree(source, tmp_path / "refused", "orchestrators/second-run")
    assert not (tmp_path / "refused").exists()
