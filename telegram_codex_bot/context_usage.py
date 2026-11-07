from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_context_usage(usage: dict[str, Any]) -> dict[str, int | None] | None:
    """Use the latest request's total, never the thread's cumulative usage."""
    last = usage.get("last") or usage.get("last_token_usage") or {}
    if not isinstance(last, dict):
        return None
    used = last.get("totalTokens", last.get("total_tokens"))
    if type(used) is not int or used < 0:
        return None
    window = usage.get("modelContextWindow", usage.get("model_context_window"))
    if type(window) is not int or window <= 0:
        window = None
    return {"used_tokens": used, "window_tokens": window}


def read_context_usage(path: Path) -> dict[str, int | None] | None:
    """Read the newest valid token_count from a rollout, starting at its tail."""
    def parse(line: bytes) -> dict[str, int | None] | None:
        if b'"token_count"' not in line:
            return None
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        return normalize_context_usage(info) if isinstance(info, dict) else None

    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        pending = b""
        while position:
            size = min(position, 64 * 1024)
            position -= size
            handle.seek(position)
            lines = (handle.read(size) + pending).split(b"\n")
            pending = lines[0]
            for line in reversed(lines[1:]):
                usage = parse(line)
                if usage is not None:
                    return usage
        return parse(pending)
