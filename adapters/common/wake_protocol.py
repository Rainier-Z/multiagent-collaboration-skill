"""Explicit contract between the coordinator and a platform wake mechanism.

This module deliberately contains no API client, credentials, scheduler, or model
prompt.  A file monitor can observe an instruction but cannot wake an LLM by
itself.  An adapter may report only whether an external platform accepted the
minimal wake request; it never represents participant execution or completion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


WakeStatus = Literal["accepted", "unavailable", "rejected"]
_VALID_STATUSES = frozenset({"accepted", "unavailable", "rejected"})


@dataclass(frozen=True)
class WakeRequest:
    """A minimal locator for one already-published instruction.

    The request intentionally has no proposal, response, discussion content,
    credentials, or hidden implementation context.  The participant must read
    its own instruction from the shared workspace after it is activated.
    """

    workspace: Path | str
    agent_id: str
    instruction_id: str
    runtime_version: str
    platform_id: str
    session_id: str

    def __post_init__(self) -> None:
        workspace = Path(self.workspace)
        if not str(workspace):
            raise ValueError("workspace is required")
        if not self.agent_id:
            raise ValueError("agent_id is required")
        if not self.instruction_id:
            raise ValueError("instruction_id is required")
        if not self.runtime_version:
            raise ValueError("runtime_version is required")
        if not isinstance(self.platform_id, str) or not self.platform_id.strip():
            raise ValueError("platform_id is required")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id is required")
        object.__setattr__(self, "workspace", workspace)

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace": str(self.workspace),
            "agent_id": self.agent_id,
            "instruction_id": self.instruction_id,
            "runtime_version": self.runtime_version,
            "platform_id": self.platform_id,
            "session_id": self.session_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WakeResult:
    """Observable handoff outcome, not a claim that the instruction ran."""

    status: WakeStatus
    detail: str = ""
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"unsupported wake status: {self.status}")

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail, "evidence": self.evidence}


class WakeAdapter(Protocol):
    """A platform-specific handoff implementation supplied to the coordinator."""

    def wake(self, request: WakeRequest) -> WakeResult:
        """Attempt one handoff; never execute participant work in this call."""


def unavailable(platform: str, detail: str = "") -> WakeResult:
    message = detail or f"{platform} has no verified external wake configuration"
    return WakeResult("unavailable", message, "no verified platform dispatch evidence")
