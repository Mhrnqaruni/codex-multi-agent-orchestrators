"""Validate Codex JSONL; human-readable stderr is not a success protocol."""

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class AgentResult:
    output: str
    error: str
    returncode: int
    session_id: str | None


def parse_events(stdout: str, stderr: str, returncode: int) -> AgentResult:
    session_id = None
    messages: list[str] = []
    completed = False
    failed = False
    errors: list[str] = []
    try:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("invalid envelope")
            kind = event["type"]
            if kind == "thread.started":
                value = event.get("thread_id")
                if not isinstance(value, str) or not value:
                    raise ValueError("missing thread id")
                session_id = value
            elif kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    raise ValueError("invalid item")
                if item.get("type") == "agent_message":
                    if not isinstance(item.get("text"), str):
                        raise ValueError("invalid message")
                    messages.append(item["text"])
            elif kind == "turn.completed":
                completed = True
            elif kind in {"turn.failed", "error"}:
                failed = True
                errors.append(json.dumps(event.get("error", event.get("message", "agent error"))))
            # Unknown well-formed event types are forward-compatible metadata.
    except (ValueError, TypeError):
        return AgentResult("", "malformed event stream", 1, session_id)
    if failed or not completed or returncode != 0:
        error = "\n".join(errors) or stderr or "incomplete event stream"
        return AgentResult("\n\n".join(messages), error, returncode or 1, session_id)
    return AgentResult("\n\n".join(messages), "", 0, session_id)
