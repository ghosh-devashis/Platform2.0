"""Record/replay store for model answers (RUN-06): deterministic LLM behaviour with no keys, cost or internet.

A recording is keyed by what determines the answer: the logical model, the messages, the tools and the tool
choice. Sampling settings are deliberately ignored, so a replay doesn't depend on `temperature`. Recordings hold
the model's response only, never the request content, and can be committed as test fixtures.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

KEY_FIELDS = ("model", "messages", "tools", "tool_choice", "response_format")


def request_key(body: dict[str, Any]) -> str:
    """Stable fingerprint of a chat completion request."""
    material = {field: body.get(field) for field in KEY_FIELDS}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RecordingStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key[:24]}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        return entry["response"] if entry.get("key") == key else None

    def save(self, key: str, model: str, response: dict[str, Any]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        entry = {"key": key, "model": model, "recorded_at": int(time.time()), "response": response}
        path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        return path
