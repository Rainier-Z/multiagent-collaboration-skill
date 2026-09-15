"""Minimal private fallback for a runtime copied outside the coordinator Skill.

The source package imports the coordinator's ``workflow_core``.  A published
Runtime intentionally does not require the full Skill to be installed for a
participant, so its copy falls back to these compatible, scoped primitives.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

E_SCHEMA = "E_SCHEMA"
E_PATH_SCOPE = "E_PATH_SCOPE"
E_HASH = "E_HASH"
E_STATE_CONFLICT = "E_STATE_CONFLICT"
E_RUNTIME_VERSION = "E_RUNTIME_VERSION"
E_OUTPUT_FORMAT = "E_OUTPUT_FORMAT"


class WorkflowError(RuntimeError):
    def __init__(self, message: str, code: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".runtime-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def path_in_workspace(workspace: Path, relative: str) -> Path:
    base = Path(workspace).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise WorkflowError("E_PATH_SCOPE: path escapes workspace", E_PATH_SCOPE, path=relative) from exc
    return candidate
