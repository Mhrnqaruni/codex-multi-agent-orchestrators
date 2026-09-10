"""Allowlisted diagnostics: never persist prompts, paths, IDs, or agent text."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from .recovery import RunBudget


def state_base() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "codex-orchestrators"


class MetadataLogger:
    """Compatibility facade; positional content arguments are not serialized.

    Per-run logs are bounded to 1000 events. Resume state and requested result
    artifacts are a separate, sensitive storage surface, not these diagnostics.
    """

    def __init__(self, identifier: str = "", prompt_log_mode: str = "off"):
        if prompt_log_mode != "off":
            raise ValueError("Content logging is not supported; use --log-prompts=off")
        self.prompt_log_mode = "off"
        self.session_dir = str(state_base() / "diagnostics" / uuid.uuid4().hex)
        self.log_dir = self.session_dir
        directory = Path(self.session_dir)
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        self._path = directory / "events.jsonl"
        self._path.touch(mode=0o600, exist_ok=False)
        self._events = 0
        self.run_budget = RunBudget()

    def _event(self, event: str, **counts: int | float) -> None:
        if self._events >= 1000:
            return
        record = {"event": event, "time": datetime.now(timezone.utc).isoformat(), **counts}
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        self._events += 1

    def log_master(self, tag: str, message: str) -> None:
        self._event("diagnostic", characters=len(message))

    master = log_master

    def log_user_query(self, query: str) -> None:
        self._event("request", characters=len(query))

    def log_output_files(self, files: list[str]) -> None:
        self._event("outputs", count=len(files))

    def log_agent_start(self, agent: str, round_num, prompt: str) -> None:
        self._event("agent_started", prompt_characters=len(prompt))

    def log_agent_output(self, agent: str, round_num, stdout: str, stderr: str,
                         returncode: int, duration: float) -> None:
        self._event("agent_finished", stdout_characters=len(stdout),
                    stderr_characters=len(stderr), returncode=returncode,
                    duration=round(duration, 3))

    def agent(self, agent: str, round_num, prompt: str, stdout: str, stderr: str,
              rc: int, duration: float) -> None:
        self.log_agent_output(agent, round_num, stdout, stderr, rc, duration)

    def log_round_summary(self, round_num: int, verdict: str) -> None:
        self._event("round_finished", round=round_num)

    def log_session_end(self, final_output: str, total_rounds: int, status: str = "completed") -> None:
        self._event("workflow_finished", rounds=total_rounds)
