#!/usr/bin/env python3
"""Protocol objects for the project-scoped Participant Runtime.

The coordinator is the sole issuer of immutable instructions.  This module
only validates, publishes, and records the append-only participant protocol;
it never mutates ``state.json`` or the discussion Markdown.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

try:
    from attestation_keys import AttestationVerificationError, verify_payload_signature
except ModuleNotFoundError:
    from ..attestation_keys import AttestationVerificationError, verify_payload_signature

try:  # Coordinator source tree: share the single authoritative core.
    from workflow_core import (
        E_HASH, E_PATH_SCOPE, E_RUNTIME_VERSION, E_SCHEMA, E_STATE_CONFLICT,
        WorkflowError, atomic_write_json, load_json, path_in_workspace, sha256_file,
    )
except ModuleNotFoundError:  # Published project Runtime: use its private scoped fallback.
    try:
        from .runtime_core import (
            E_HASH, E_PATH_SCOPE, E_RUNTIME_VERSION, E_SCHEMA, E_STATE_CONFLICT,
            WorkflowError, atomic_write_json, load_json, path_in_workspace, sha256_file,
        )
    except ImportError:
        from runtime_core import (  # type: ignore[no-redef]
            E_HASH, E_PATH_SCOPE, E_RUNTIME_VERSION, E_SCHEMA, E_STATE_CONFLICT,
            WorkflowError, atomic_write_json, load_json, path_in_workspace, sha256_file,
        )

RUNTIME_VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0"
E_ISOLATION_UNVERIFIED = "E_ISOLATION_UNVERIFIED"
E_ISOLATION_EVIDENCE = E_ISOLATION_UNVERIFIED
AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
INSTRUCTION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
INSTRUCTION_KINDS = frozenset({"bootstrap", "propose", "respond", "repair", "upgrade", "stop"})
TERMINAL_STATUSES = frozenset({"completed", "failed"})
ENFORCEMENT_TYPES = frozenset({"platform_sandbox", "process_allowlist", "separate_os_identity"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ATTESTATION_CLOCK_SKEW = timedelta(minutes=5)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _error(message: str, code: str, **details: Any) -> WorkflowError:
    return WorkflowError("%s: %s" % (code, message), code, **details)


def _require_string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise _error("field %s must be a non-empty string" % field, E_SCHEMA, field=field)
    return item


def _require_positive_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise _error("field %s must be a positive integer" % field, E_SCHEMA, field=field)
    return item


def _validate_agent_id(agent_id: str) -> str:
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise _error("invalid agent id" , E_SCHEMA, agent_id=agent_id)
    return agent_id


def validate_agent_id(agent_id: str) -> str:
    return _validate_agent_id(agent_id)


def validate_instruction_id(instruction_id: str) -> str:
    if not INSTRUCTION_ID_RE.fullmatch(instruction_id):
        raise _error("invalid instruction id", E_SCHEMA, instruction_id=instruction_id)
    return instruction_id


def input_scope_digest(agent_id: str, files: list[dict[str, str]] | tuple[dict[str, str], ...]) -> str:
    """Digest the exact participant view and input path/content hash set."""
    view_root = ".multiagent/views/%s" % _validate_agent_id(agent_id)
    normalized = sorted(
        ({"path": item["path"], "sha256": item["sha256"]} for item in files),
        key=lambda item: item["path"],
    )
    return hashlib.sha256(_canonical_json({
        "agent_id": agent_id,
        "view_root": view_root,
        "files": normalized,
    })).hexdigest()


@dataclass(frozen=True)
class InputManifest:
    agent_id: str
    view_root: str
    files: tuple[tuple[str, str], ...]
    scope_digest: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InputManifest":
        agent_id = _validate_agent_id(_require_string(value, "agent_id"))
        view_root = _require_string(value, "view_root")
        expected_root = ".multiagent/views/%s" % agent_id
        if view_root != expected_root:
            raise _error("input manifest view_root does not match agent", E_PATH_SCOPE, agent_id=agent_id)
        files = value.get("files")
        if not isinstance(files, list):
            raise _error("input manifest files must be a list", E_SCHEMA)
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise _error("input manifest entry must be an object", E_SCHEMA)
            path = _require_string(item, "path")
            digest = _require_string(item, "sha256")
            if not SHA256_RE.fullmatch(digest):
                raise _error("input manifest sha256 must be lowercase SHA-256", E_SCHEMA, path=path)
            posix_path = PurePosixPath(path)
            windows_path = PureWindowsPath(path)
            if (
                posix_path.is_absolute() or windows_path.is_absolute()
                or ".." in posix_path.parts or ".." in windows_path.parts
                or "\\" in path or not path.startswith(expected_root + "/inputs/")
            ):
                raise _error("input manifest path is outside participant view", E_PATH_SCOPE, path=path)
            if path in seen:
                raise _error("input manifest contains duplicate paths", E_SCHEMA, path=path)
            seen.add(path)
            normalized.append((path, digest))
        scope_digest = _require_string(value, "scope_digest")
        if not SHA256_RE.fullmatch(scope_digest):
            raise _error("input manifest scope_digest must be lowercase SHA-256", E_SCHEMA)
        expected_digest = input_scope_digest(
            agent_id,
            [{"path": path, "sha256": digest} for path, digest in normalized],
        )
        if scope_digest != expected_digest:
            raise _error("input manifest scope_digest mismatch", E_HASH, expected=expected_digest, actual=scope_digest)
        return cls(agent_id, view_root, tuple(normalized), scope_digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "view_root": self.view_root,
            "files": [{"path": path, "sha256": digest} for path, digest in self.files],
            "scope_digest": self.scope_digest,
        }


def _validate_participant_scope(
    agent_id: str,
    kind: str,
    input_paths: list[str],
    output_path: str,
    access_scope: dict[str, Any],
) -> None:
    view_root = ".multiagent/views/%s" % agent_id
    receipt_root = ".multiagent/receipts/%s" % agent_id
    expected_scope = {
        "mode": "sealed_view",
        "view_root": view_root,
        "allowed_read_roots": [view_root + "/inputs"],
        "allowed_write_roots": [view_root + "/outputs", receipt_root],
    }
    security_mode = access_scope.get("security_mode", "strict")
    if security_mode not in {"normal", "strict"}:
        raise _error("access_scope security_mode is invalid", E_SCHEMA)
    for field, expected in expected_scope.items():
        if access_scope.get(field) != expected:
            raise _error("access_scope %s does not match the participant view" % field, E_SCHEMA, field=field)
    if access_scope.get("requires_platform_enforcement") is not True:
        raise _error("access_scope must require platform enforcement", E_SCHEMA)
    if access_scope.get("independence_claim_requires_enforcement_receipt") is not (security_mode == "strict"):
        raise _error("access_scope must require enforcement evidence for independence claims", E_SCHEMA)
    manifest_hash = access_scope.get("input_manifest_sha256")
    scope_digest = access_scope.get("scope_digest")
    manifest_path = access_scope.get("input_manifest_path")
    if (
        not isinstance(manifest_hash, str) or not SHA256_RE.fullmatch(manifest_hash)
        or not isinstance(scope_digest, str) or not SHA256_RE.fullmatch(scope_digest)
        or manifest_path != input_manifest_path(agent_id, manifest_hash)
    ):
        raise _error("access_scope must reference its immutable input manifest", E_SCHEMA, agent_id=agent_id)
    security_note = access_scope.get("security_note")
    if not isinstance(security_note, str) or not security_note.strip():
        raise _error("access_scope must explain the platform-enforcement boundary", E_SCHEMA)
    verifier = access_scope.get("attestation_verifier")
    if verifier is not None and (
        not isinstance(verifier, dict)
        or verifier.get("algorithm") != "ed25519"
        or not isinstance(verifier.get("key_id"), str) or not verifier["key_id"].strip()
        or not isinstance(verifier.get("public_key_b64"), str) or not verifier["public_key_b64"].strip()
    ):
        raise _error("access_scope attestation_verifier must bind an Ed25519 key id and public key", E_ISOLATION_UNVERIFIED)

    input_root = view_root + "/inputs/"
    for item in input_paths:
        posix_path = PurePosixPath(item)
        windows_path = PureWindowsPath(item)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or ".." in posix_path.parts
            or ".." in windows_path.parts
            or "\\" in item
            or not item.startswith(input_root)
        ):
            raise _error("input path is outside the participant sealed view", E_PATH_SCOPE, path=item, agent_id=agent_id)

    expected_outputs = {
        "propose": view_root + "/outputs/提案文档.md",
        "repair": view_root + "/outputs/提案文档.md",
        "respond": view_root + "/outputs/交叉回应文档.md",
    }
    expected_output = expected_outputs.get(kind, receipt_root)
    response_round_path = (
        kind == "respond"
        and re.fullmatch(
            rf"{re.escape(view_root)}/outputs/round-[1-9][0-9]*/交叉回应文档\.md",
            output_path.rstrip("/"),
        )
    )
    if output_path.rstrip("/") != expected_output and not response_round_path:
        raise _error("output path is outside the participant write scope", E_PATH_SCOPE, path=output_path, agent_id=agent_id)


def input_manifest_path(agent_id: str, manifest_sha256: str) -> str:
    return ".multiagent/views/%s/inputs/.manifests/%s.json" % (
        _validate_agent_id(agent_id), manifest_sha256,
    )


@dataclass(frozen=True)
class Instruction:
    instruction_id: str
    discussion_id: str
    sequence: int
    kind: str
    agent_id: str
    runtime_version: str
    state_revision: int
    task_prompt: str
    input_paths: tuple[str, ...]
    output_path: str
    access_scope: dict[str, Any]
    attempt: int
    max_attempts: int
    issued_at: str
    sha256: str
    origin_instruction_id: str | None = None
    root_instruction_id: str | None = None
    operational_directive: str | None = None
    platform_id: str | None = None
    session_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Instruction":
        required = ("instruction_id", "discussion_id", "sequence", "kind", "agent_id", "runtime_version", "state_revision", "task_prompt", "input_paths", "output_path", "access_scope", "attempt", "max_attempts", "issued_at", "sha256")
        for field in required:
            if field not in value:
                raise _error("missing required instruction field %s" % field, E_SCHEMA, field=field)
        kind = _require_string(value, "kind")
        if kind not in INSTRUCTION_KINDS:
            raise _error("unsupported instruction kind", E_SCHEMA, kind=kind)
        agent_id = _validate_agent_id(_require_string(value, "agent_id"))
        instruction_id = validate_instruction_id(_require_string(value, "instruction_id"))
        discussion_id = _require_string(value, "discussion_id")
        if not discussion_id.strip():
            raise _error("discussion_id must be a non-empty string", E_SCHEMA, field="discussion_id")
        paths = value["input_paths"]
        if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
            raise _error("input_paths must be a list of non-empty strings", E_SCHEMA)
        task_prompt = _require_string(value, "task_prompt")
        if len(task_prompt.strip()) < 80:
            raise _error("task_prompt must contain a complete executable instruction", E_SCHEMA)
        output_path = _require_string(value, "output_path")
        if agent_id not in task_prompt or output_path not in task_prompt or any(path not in task_prompt for path in paths):
            raise _error("task_prompt must bind the participant, every input and its output", E_SCHEMA)
        access_scope = value["access_scope"]
        if not isinstance(access_scope, dict):
            raise _error("access_scope must be an object", E_SCHEMA)
        _validate_participant_scope(agent_id, kind, paths, output_path, access_scope)
        checksum = _require_string(value, "sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise _error("instruction sha256 must be lowercase SHA-256", E_SCHEMA)
        origin = value.get("origin_instruction_id")
        root = value.get("root_instruction_id")
        if origin is not None and (not isinstance(origin, str) or not origin):
            raise _error("origin_instruction_id must be a non-empty string", E_SCHEMA)
        if root is not None and (not isinstance(root, str) or not root):
            raise _error("root_instruction_id must be a non-empty string", E_SCHEMA)
        operational_directive = value.get("operational_directive")
        if operational_directive is not None and not isinstance(operational_directive, str):
            raise _error("operational_directive must be a string", E_SCHEMA)
        platform_id = value.get("platform_id")
        session_id = value.get("session_id")
        if (platform_id is None) != (session_id is None):
            raise _error("platform_id and session_id must be supplied together", E_SCHEMA)
        if platform_id is not None and (
            not isinstance(platform_id, str) or not platform_id.strip()
            or not isinstance(session_id, str) or not session_id.strip()
        ):
            raise _error("platform_id and session_id must be non-empty strings", E_SCHEMA)
        if agent_id == "openclaw" and (not isinstance(operational_directive, str) or not operational_directive.strip()):
            raise _error(
                "openclaw instructions require a non-empty operational_directive",
                E_SCHEMA,
                agent_id=agent_id,
                field="operational_directive",
            )
        instruction = cls(
            instruction_id=instruction_id,
            discussion_id=discussion_id,
            sequence=_require_positive_int(value, "sequence"),
            kind=kind,
            agent_id=agent_id,
            runtime_version=_require_string(value, "runtime_version"),
            state_revision=_require_positive_int(value, "state_revision"),
            task_prompt=task_prompt,
            input_paths=tuple(paths),
            output_path=_require_string(value, "output_path"),
            access_scope=dict(access_scope),
            attempt=_require_positive_int(value, "attempt"),
            max_attempts=_require_positive_int(value, "max_attempts"),
            issued_at=_require_string(value, "issued_at"),
            sha256=checksum,
            origin_instruction_id=origin,
            root_instruction_id=root,
            operational_directive=operational_directive,
            platform_id=platform_id,
            session_id=session_id,
        )
        if instruction.attempt > instruction.max_attempts:
            raise _error("attempt cannot exceed max_attempts", E_SCHEMA)
        if not instruction.verify_sha256():
            raise _error("instruction payload checksum mismatch", E_HASH, instruction_id=instruction.instruction_id)
        return instruction

    def payload_without_hash(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instruction_id": self.instruction_id,
            "discussion_id": self.discussion_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "agent_id": self.agent_id,
            "runtime_version": self.runtime_version,
            "state_revision": self.state_revision,
            "task_prompt": self.task_prompt,
            "input_paths": list(self.input_paths),
            "output_path": self.output_path,
            "access_scope": self.access_scope,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "issued_at": self.issued_at,
        }
        if self.origin_instruction_id is not None:
            payload["origin_instruction_id"] = self.origin_instruction_id
        if self.root_instruction_id is not None:
            payload["root_instruction_id"] = self.root_instruction_id
        if self.operational_directive is not None:
            payload["operational_directive"] = self.operational_directive
        if self.platform_id is not None and self.session_id is not None:
            payload["platform_id"] = self.platform_id
            payload["session_id"] = self.session_id
        return payload

    def verify_sha256(self) -> bool:
        return hashlib.sha256(_canonical_json(self.payload_without_hash())).hexdigest() == self.sha256

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload_without_hash()
        payload["sha256"] = self.sha256
        return payload


def validate_input_manifest(workspace: Path, instruction: Instruction) -> InputManifest:
    """Verify the manifest reference and the current bytes of every sealed input."""
    scope = instruction.access_scope
    manifest_path_value = scope.get("input_manifest_path")
    manifest_hash = scope.get("input_manifest_sha256")
    scope_digest = scope.get("scope_digest")
    if not isinstance(manifest_path_value, str) or not isinstance(manifest_hash, str):
        raise _error("instruction has no immutable input manifest reference", E_SCHEMA)
    manifest_path = path_in_workspace(workspace, manifest_path_value)
    if not manifest_path.is_file() or sha256_file(manifest_path) != manifest_hash:
        raise _error("sealed input manifest hash mismatch", E_HASH, path=manifest_path_value)
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error("sealed input manifest cannot be read", E_HASH, path=manifest_path_value) from exc
    if not isinstance(value, dict):
        raise _error("sealed input manifest must be an object", E_SCHEMA)
    manifest = InputManifest.from_dict(value)
    if manifest.agent_id != instruction.agent_id:
        raise _error("sealed input manifest belongs to another agent", E_PATH_SCOPE, agent_id=manifest.agent_id)
    if manifest.scope_digest != scope_digest:
        raise _error("access_scope scope_digest does not match input manifest", E_HASH)
    if {path for path, _ in manifest.files} != set(instruction.input_paths):
        raise _error("input manifest paths do not match the instruction", E_HASH)
    for relative, expected_hash in manifest.files:
        target = path_in_workspace(workspace, relative)
        if not target.is_file() or sha256_file(target) != expected_hash:
            raise _error("sealed input content hash mismatch", E_HASH, path=relative)
    return manifest


@dataclass(frozen=True)
class IsolationEvidence:
    agent_id: str
    instruction_id: str
    platform_id: str
    session_id: str
    view_root: str
    scope_digest: str
    input_manifest_sha256: str
    enforcement_type: str
    issued_at: str
    allowed_read_roots: tuple[str, ...]
    allowed_write_roots: tuple[str, ...]
    evidence_sha256: str
    verifier: dict[str, Any] | None = None
    trust: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IsolationEvidence":
        if value.get("issuer") != "platform-attestation" or value.get("source") != "platform-attestation":
            raise _error("isolation evidence must be a platform attestation", E_ISOLATION_UNVERIFIED)
        evidence_type = _require_string(value, "evidence_type")
        enforcement_type = _require_string(value, "enforcement_type")
        if evidence_type not in ENFORCEMENT_TYPES or enforcement_type != evidence_type:
            raise _error("unsupported isolation enforcement type", E_ISOLATION_UNVERIFIED, enforcement_type=enforcement_type)
        issued_at = _require_string(value, "issued_at")
        try:
            parsed_time = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _error("isolation evidence issued_at must be ISO 8601", E_ISOLATION_UNVERIFIED) from exc
        if parsed_time.tzinfo is None:
            raise _error("isolation evidence issued_at must include a timezone", E_ISOLATION_UNVERIFIED)
        read_roots = value.get("allowed_read_roots")
        write_roots = value.get("allowed_write_roots")
        if not isinstance(read_roots, list) or not all(isinstance(item, str) for item in read_roots):
            raise _error("isolation evidence allowed_read_roots must be a string list", E_ISOLATION_UNVERIFIED)
        if not isinstance(write_roots, list) or not all(isinstance(item, str) for item in write_roots):
            raise _error("isolation evidence allowed_write_roots must be a string list", E_ISOLATION_UNVERIFIED)
        verifier = value.get("verifier")
        trust = value.get("trust")
        if verifier is not None and not isinstance(verifier, dict):
            raise _error("isolation evidence verifier must be an object", E_ISOLATION_UNVERIFIED)
        if trust is not None and not isinstance(trust, dict):
            raise _error("isolation evidence trust must be an object", E_ISOLATION_UNVERIFIED)
        return cls(
            agent_id=_require_string(value, "agent_id"),
            instruction_id=_require_string(value, "instruction_id"),
            platform_id=_require_string(value, "platform_id"),
            session_id=_require_string(value, "session_id"),
            view_root=_require_string(value, "view_root"),
            scope_digest=_require_string(value, "scope_digest"),
            input_manifest_sha256=_require_string(value, "input_manifest_sha256"),
            enforcement_type=enforcement_type,
            issued_at=issued_at,
            allowed_read_roots=tuple(read_roots),
            allowed_write_roots=tuple(write_roots),
            evidence_sha256=hashlib.sha256(_canonical_json(value)).hexdigest(),
            verifier=dict(verifier) if verifier is not None else None,
            trust=dict(trust) if trust is not None else None,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "mode": "platform_enforced",
            "agent_id": self.agent_id,
            "instruction_id": self.instruction_id,
            "platform_id": self.platform_id,
            "session_id": self.session_id,
            "view_root": self.view_root,
            "scope_digest": self.scope_digest,
            "input_manifest_sha256": self.input_manifest_sha256,
            "evidence_type": self.enforcement_type,
            "enforcement_type": self.enforcement_type,
            "issued_at": self.issued_at,
            "evidence_sha256": self.evidence_sha256,
            "allowed_read_roots": list(self.allowed_read_roots),
            "allowed_write_roots": list(self.allowed_write_roots),
            "contract_validation": "passed",
            "cryptographic_verification": "passed",
            "authenticity": "ed25519-verified",
            "trust_root_id": (self.trust or {}).get("trust_root_id"),
        }


def validate_isolation_evidence(value: dict[str, Any], instruction: Instruction) -> dict[str, Any]:
    """Verify signed platform evidence against the instruction pin and trust registry."""
    if instruction.platform_id is None or instruction.session_id is None:
        raise _error("strict isolation requires a bound target platform and session", E_ISOLATION_UNVERIFIED)
    verifier = instruction.access_scope.get("attestation_verifier")
    if not isinstance(verifier, dict):
        raise _error("instruction has no pinned attestation verifier", E_ISOLATION_UNVERIFIED)
    try:
        verified_key_id = verify_payload_signature(
            value,
            expected_key_id=str(verifier.get("key_id", "")),
            expected_public_key_b64=str(verifier.get("public_key_b64", "")),
        )
    except AttestationVerificationError as exc:
        raise _error("isolation evidence signature is untrusted or invalid", E_ISOLATION_UNVERIFIED) from exc
    evidence = IsolationEvidence.from_dict(value)
    scope = instruction.access_scope
    expected = {
        "agent_id": instruction.agent_id,
        "instruction_id": instruction.instruction_id,
        "platform_id": instruction.platform_id,
        "session_id": instruction.session_id,
        "view_root": scope.get("view_root"),
        "scope_digest": scope.get("scope_digest"),
        "input_manifest_sha256": scope.get("input_manifest_sha256"),
        "allowed_read_roots": tuple(scope.get("allowed_read_roots", [])),
        "allowed_write_roots": tuple(scope.get("allowed_write_roots", [])),
    }
    actual = {
        "agent_id": evidence.agent_id,
        "instruction_id": evidence.instruction_id,
        "platform_id": evidence.platform_id,
        "session_id": evidence.session_id,
        "view_root": evidence.view_root,
        "scope_digest": evidence.scope_digest,
        "input_manifest_sha256": evidence.input_manifest_sha256,
        "allowed_read_roots": evidence.allowed_read_roots,
        "allowed_write_roots": evidence.allowed_write_roots,
    }
    if actual != expected:
        raise _error("isolation evidence does not match the instruction scope", E_ISOLATION_UNVERIFIED)
    summary = evidence.summary()
    signature = value["signature"]
    summary["key_id"] = verified_key_id
    summary["signature_algorithm"] = signature["algorithm"]
    summary["trust_root_id"] = verified_key_id
    summary["attestation_verifier"] = verifier
    summary["signed_evidence"] = value
    return summary


def isolation_evidence_path(workspace: Path, instruction: Instruction) -> Path:
    return path_in_workspace(
        workspace,
        ".multiagent/audit/platform-evidence/%s/%s.json" % (instruction.agent_id, instruction.instruction_id),
    )


def load_isolation_evidence(workspace: Path, instruction: Instruction) -> dict[str, Any]:
    path = isolation_evidence_path(workspace, instruction)
    if not path.is_file():
        raise _error("platform isolation evidence is missing", E_ISOLATION_UNVERIFIED, path=str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error("platform isolation evidence is unreadable", E_ISOLATION_UNVERIFIED, path=str(path)) from exc
    if not isinstance(value, dict):
        raise _error("platform isolation evidence must be an object", E_ISOLATION_UNVERIFIED)
    return validate_isolation_evidence(value, instruction)


def stop_attestation_path(workspace: Path, instruction: Instruction) -> Path:
    return path_in_workspace(
        workspace,
        ".multiagent/audit/platform-evidence/%s/%s-stop.json"
        % (instruction.agent_id, instruction.instruction_id),
    )


def _parse_attestation_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _error("stop attestation %s must be a timestamp" % field, E_ISOLATION_UNVERIFIED)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("stop attestation %s must be ISO 8601" % field, E_ISOLATION_UNVERIFIED) from exc
    if parsed.tzinfo is None:
        raise _error("stop attestation %s must include a timezone" % field, E_ISOLATION_UNVERIFIED)
    return parsed


def _validate_stop_time_bounds(value: dict[str, Any], instruction: Instruction) -> datetime:
    issued_at = _parse_attestation_time(instruction.issued_at, "instruction.issued_at")
    stopped_at = _parse_attestation_time(value.get("stopped_at"), "stopped_at")
    latest_allowed = datetime.now(timezone.utc) + MAX_ATTESTATION_CLOCK_SKEW
    if stopped_at < issued_at or stopped_at.astimezone(timezone.utc) > latest_allowed:
        raise _error("stop attestation time is outside the instruction/current-time window", E_ISOLATION_UNVERIFIED)
    if instruction.platform_id and instruction.platform_id.casefold() == "openclaw":
        removal_checked_at = _parse_attestation_time(value.get("removal_checked_at"), "removal_checked_at")
        if (
            removal_checked_at < stopped_at
            or removal_checked_at.astimezone(timezone.utc) > latest_allowed
        ):
            raise _error("OpenClaw removal check time is outside the stop/current-time window", E_ISOLATION_UNVERIFIED)
    return stopped_at


def _validate_stop_proof_reference(reference: Any) -> str:
    prefix = ".multiagent/audit/platform-evidence/"
    if not isinstance(reference, str) or not reference.strip():
        raise _error("stop proof_reference must be a workspace-relative evidence path", E_ISOLATION_UNVERIFIED)
    posix_path = PurePosixPath(reference)
    windows_path = PureWindowsPath(reference)
    parts = reference.split("/")
    if (
        not reference.startswith(prefix)
        or reference.endswith("/")
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or "\\" in reference
        or ":" in reference
        or ".." in parts
        or "." in parts
        or "" in parts
    ):
        raise _error("stop proof_reference is outside platform-evidence", E_ISOLATION_UNVERIFIED)
    return reference


def _is_resolved_path_within(path: Path, root: Path) -> bool:
    """Return whether path remains beneath root after resolving links and '..'."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _stop_proof_path(workspace: Path, reference: Any) -> Path:
    reference = _validate_stop_proof_reference(reference)
    try:
        workspace_root = Path(workspace).resolve()
        evidence_root = (workspace_root / ".multiagent/audit/platform-evidence").resolve()
        path = path_in_workspace(workspace_root, reference)
    except (WorkflowError, OSError, RuntimeError, ValueError) as exc:
        raise _error("stop proof_reference escapes the workspace", E_ISOLATION_UNVERIFIED) from exc
    if (
        not _is_resolved_path_within(evidence_root, workspace_root)
        or not _is_resolved_path_within(path, evidence_root)
    ):
        raise _error("stop proof_reference escapes platform-evidence", E_ISOLATION_UNVERIFIED)
    if not path.is_file():
        raise _error("stop proof artifact is missing", E_ISOLATION_UNVERIFIED, path=reference)
    return path


def _validate_stop_proof_artifact(workspace: Path, value: dict[str, Any]) -> None:
    proof_path = _stop_proof_path(workspace, value.get("proof_reference"))
    proof_sha256 = value.get("proof_sha256")
    if not isinstance(proof_sha256, str) or not SHA256_RE.fullmatch(proof_sha256):
        raise _error("stop proof_sha256 must be lowercase SHA-256", E_ISOLATION_UNVERIFIED)
    try:
        proof_bytes = proof_path.read_bytes()
        if hashlib.sha256(proof_bytes).hexdigest() != proof_sha256:
            raise _error("stop proof artifact hash mismatch", E_ISOLATION_UNVERIFIED)
        proof = json.loads(proof_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("stop proof artifact is unreadable JSON", E_ISOLATION_UNVERIFIED) from exc
    if not isinstance(proof, dict):
        raise _error("stop proof artifact must be a JSON object", E_ISOLATION_UNVERIFIED)
    fields = (
        "discussion_id", "instruction_id", "instruction_sha256", "agent_id", "platform_id", "session_id",
        "stopped_at", "mechanism", "target", "action_verified",
    )
    if value.get("platform_id", "").casefold() == "openclaw":
        fields += ("automation_job_id", "removal_verified", "removal_checked_at")
    if any(proof.get(field) != value.get(field) for field in fields):
        raise _error("stop proof artifact does not match its signed attestation", E_ISOLATION_UNVERIFIED)


def validate_stop_attestation(value: dict[str, Any], instruction: Instruction, workspace: Path) -> dict[str, Any]:
    """Verify signed proof that this bound platform/session actually stopped."""
    verifier = instruction.access_scope.get("attestation_verifier")
    if instruction.platform_id is None or instruction.session_id is None or not isinstance(verifier, dict):
        raise _error("stop instruction has no pinned platform/session verifier", E_ISOLATION_UNVERIFIED)
    try:
        key_id = verify_payload_signature(
            value,
            expected_key_id=str(verifier.get("key_id", "")),
            expected_public_key_b64=str(verifier.get("public_key_b64", "")),
        )
    except AttestationVerificationError as exc:
        raise _error("stop attestation signature is untrusted or invalid", E_ISOLATION_UNVERIFIED) from exc
    if any((
        value.get("discussion_id") != instruction.discussion_id,
        value.get("agent_id") != instruction.agent_id,
        value.get("instruction_id") != instruction.instruction_id,
        value.get("instruction_sha256") != instruction.sha256,
        value.get("platform_id") != instruction.platform_id,
        value.get("session_id") != instruction.session_id,
        value.get("action_verified") is not True,
    )):
        raise _error("stop attestation identity/action does not match the instruction", E_ISOLATION_UNVERIFIED)
    _validate_stop_time_bounds(value, instruction)
    for field in ("discussion_id", "mechanism", "target"):
        _require_string(value, field)
    _validate_stop_proof_artifact(Path(workspace), value)
    if instruction.platform_id.casefold() == "openclaw":
        _require_string(value, "automation_job_id")
        if value.get("removal_verified") is not True:
            raise _error("OpenClaw stop attestation must verify automation removal", E_ISOLATION_UNVERIFIED)
    evidence_hash = hashlib.sha256(_canonical_json(value)).hexdigest()
    signature = value["signature"]
    return {
        "mode": "platform_platform_stop_verified",
        "agent_id": instruction.agent_id,
        "discussion_id": instruction.discussion_id,
        "instruction_id": instruction.instruction_id,
        "instruction_sha256": instruction.sha256,
        "platform_id": instruction.platform_id,
        "session_id": instruction.session_id,
        "stopped_at": value["stopped_at"],
        "mechanism": value["mechanism"],
        "target": value["target"],
        "proof_reference": value["proof_reference"],
        "proof_sha256": value["proof_sha256"],
        "action_verified": True,
        "evidence_sha256": evidence_hash,
        "key_id": key_id,
        "signature_algorithm": signature["algorithm"],
        "cryptographic_verification": "passed",
        "authenticity": "ed25519-verified",
        "contract_validation": "passed",
        "attestation_verifier": verifier,
        "signed_evidence": value,
        **({
            "automation_job_id": value["automation_job_id"],
            "removal_verified": True,
            "removal_checked_at": value["removal_checked_at"],
        } if instruction.platform_id.casefold() == "openclaw" else {}),
    }


def load_stop_attestation(workspace: Path, instruction: Instruction) -> dict[str, Any]:
    path = stop_attestation_path(workspace, instruction)
    if not path.is_file():
        raise _error("signed platform stop attestation is missing", E_ISOLATION_UNVERIFIED, path=str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error("signed platform stop attestation is unreadable", E_ISOLATION_UNVERIFIED, path=str(path)) from exc
    if not isinstance(value, dict):
        raise _error("signed platform stop attestation must be an object", E_ISOLATION_UNVERIFIED)
    return validate_stop_attestation(value, instruction, workspace)


@dataclass(frozen=True)
class Receipt:
    payload: dict[str, Any]

    @property
    def instruction_id(self) -> str:
        return str(self.payload["instruction_id"])

    @property
    def agent_id(self) -> str:
        return str(self.payload["agent_id"])

    @property
    def status(self) -> str:
        return str(self.payload["status"])

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Receipt":
        required = ("instruction_id", "agent_id", "runtime_version", "state_revision", "status", "at", "attempt")
        for field in required:
            _require_string(value, field) if field not in {"state_revision", "attempt"} else _require_positive_int(value, field)
        validate_instruction_id(_require_string(value, "instruction_id"))
        status = _require_string(value, "status")
        if status not in {"accepted", "completed", "failed"}:
            raise _error("unsupported receipt status", E_SCHEMA, status=status)
        _validate_agent_id(_require_string(value, "agent_id"))
        if status == "completed":
            _require_string(value, "output_path")
            output_hash = _require_string(value, "output_sha256")
            if not re.fullmatch(r"[0-9a-f]{64}", output_hash):
                raise _error("completed receipt output_sha256 must be lowercase SHA-256", E_SCHEMA)
        if status in {"accepted", "completed"}:
            kind = _require_string(value, "kind")
            if kind not in INSTRUCTION_KINDS:
                raise _error("receipt kind is unsupported", E_SCHEMA, kind=kind)
            summary = value.get("isolation_evidence")
            if not isinstance(summary, dict):
                raise _error("accepted/completed receipt requires isolation evidence summary", E_ISOLATION_UNVERIFIED)
            _validate_receipt_evidence_summary(
                summary,
                _require_string(value, "agent_id"),
                _require_string(value, "instruction_id"),
                kind,
                _require_string(value, "at"),
            )
        if status == "failed":
            _require_string(value, "error_code")
            _require_string(value, "message")
            if not isinstance(value.get("recoverable"), bool):
                raise _error("failed receipt recoverable must be boolean", E_SCHEMA)
        return cls(dict(value))


def _validate_receipt_evidence_summary(
    summary: dict[str, Any], agent_id: str, instruction_id: str, kind: str, receipt_at: str,
) -> None:
    view_root = ".multiagent/views/%s" % agent_id
    expected_read_roots = [view_root + "/inputs"]
    expected_write_roots = [view_root + "/outputs", ".multiagent/receipts/%s" % agent_id]
    if summary.get("agent_id") != agent_id or summary.get("instruction_id") != instruction_id:
        raise _error("receipt isolation evidence summary identity mismatch", E_ISOLATION_UNVERIFIED)
    if summary.get("contract_validation") != "passed":
        raise _error("receipt isolation evidence contract was not validated", E_ISOLATION_UNVERIFIED)
    if kind in {"propose", "repair"}:
        signed_evidence = summary.get("signed_evidence")
        verifier = summary.get("attestation_verifier")
        if not isinstance(signed_evidence, dict) or not isinstance(verifier, dict):
            raise _error("strict proposal receipt lacks signed evidence and verifier pin", E_ISOLATION_UNVERIFIED)
        key_id = verifier.get("key_id")
        public_key = verifier.get("public_key_b64")
        if (
            verifier.get("algorithm") != "ed25519"
            or not isinstance(key_id, str) or not key_id
            or not isinstance(public_key, str) or not public_key
            or summary.get("key_id") != key_id
        ):
            raise _error("strict proposal receipt verifier pin is invalid", E_ISOLATION_UNVERIFIED)
        try:
            verify_payload_signature(
                signed_evidence,
                expected_key_id=key_id,
                expected_public_key_b64=public_key,
            )
            IsolationEvidence.from_dict(signed_evidence)
        except AttestationVerificationError as exc:
            raise _error("receipt isolation attestation signature is invalid", E_ISOLATION_UNVERIFIED) from exc
        except WorkflowError as exc:
            if exc.code == E_ISOLATION_UNVERIFIED:
                raise
            raise _error("receipt isolation attestation is invalid", E_ISOLATION_UNVERIFIED) from exc
        if (
            signed_evidence.get("agent_id") != agent_id
            or signed_evidence.get("instruction_id") != instruction_id
            or signed_evidence.get("platform_id") != summary.get("platform_id")
            or signed_evidence.get("session_id") != summary.get("session_id")
            or signed_evidence.get("scope_digest") != summary.get("scope_digest")
            or signed_evidence.get("input_manifest_sha256") != summary.get("input_manifest_sha256")
            or signed_evidence.get("enforcement_type") != summary.get("enforcement_type")
            or hashlib.sha256(_canonical_json(signed_evidence)).hexdigest() != summary.get("evidence_sha256")
        ):
            raise _error("receipt summary does not match its signed isolation evidence", E_ISOLATION_UNVERIFIED)
        if (
            summary.get("mode") != "platform_enforced"
            or summary.get("authenticity") != "ed25519-verified"
            or summary.get("evidence_type") not in ENFORCEMENT_TYPES
            or summary.get("enforcement_type") != summary.get("evidence_type")
            or summary.get("cryptographic_verification") != "passed"
            or summary.get("signature_algorithm") != "ed25519"
            or not isinstance(summary.get("key_id"), str) or not summary["key_id"].strip()
            or not isinstance(summary.get("evidence_sha256"), str)
            or not SHA256_RE.fullmatch(summary["evidence_sha256"])
            or summary.get("view_root") != view_root
            or summary.get("allowed_read_roots") != expected_read_roots
            or summary.get("allowed_write_roots") != expected_write_roots
            or not SHA256_RE.fullmatch(str(summary.get("scope_digest", "")))
            or not SHA256_RE.fullmatch(str(summary.get("input_manifest_sha256", "")))
            or not all(isinstance(summary.get(field), str) and summary[field].strip() for field in (
                "platform_id", "session_id", "view_root", "scope_digest", "input_manifest_sha256", "issued_at",
            ))
            or not isinstance(summary.get("allowed_read_roots"), list)
            or not isinstance(summary.get("allowed_write_roots"), list)
        ):
            raise _error("strict proposal receipt lacks a valid platform evidence summary", E_ISOLATION_UNVERIFIED)
        return
    if kind == "stop":
        signed_evidence = summary.get("signed_evidence")
        verifier = summary.get("attestation_verifier")
        if not isinstance(signed_evidence, dict) or not isinstance(verifier, dict):
            raise _error("stop receipt lacks signed platform attestation", E_ISOLATION_UNVERIFIED)
        key_id = verifier.get("key_id")
        public_key = verifier.get("public_key_b64")
        if (
            verifier.get("algorithm") != "ed25519"
            or not isinstance(key_id, str) or not key_id
            or not isinstance(public_key, str) or not public_key
        ):
            raise _error("stop receipt verifier pin is invalid", E_ISOLATION_UNVERIFIED)
        try:
            verify_payload_signature(
                signed_evidence,
                expected_key_id=key_id,
                expected_public_key_b64=public_key,
            )
        except AttestationVerificationError as exc:
            raise _error("stop receipt signature is invalid", E_ISOLATION_UNVERIFIED) from exc
        if any((
            signed_evidence.get("agent_id") != agent_id,
            not isinstance(signed_evidence.get("agent_id"), str)
            or not signed_evidence["agent_id"].strip(),
            not isinstance(signed_evidence.get("discussion_id"), str)
            or not signed_evidence["discussion_id"].strip(),
            summary.get("discussion_id") != signed_evidence.get("discussion_id"),
            signed_evidence.get("instruction_id") != instruction_id,
            summary.get("instruction_sha256") != signed_evidence.get("instruction_sha256"),
            not isinstance(signed_evidence.get("instruction_sha256"), str)
            or not SHA256_RE.fullmatch(signed_evidence["instruction_sha256"]),
            not isinstance(signed_evidence.get("instruction_id"), str)
            or not signed_evidence["instruction_id"].strip(),
            signed_evidence.get("platform_id") != summary.get("platform_id"),
            not isinstance(signed_evidence.get("platform_id"), str)
            or not signed_evidence["platform_id"].strip(),
            signed_evidence.get("session_id") != summary.get("session_id"),
            not isinstance(signed_evidence.get("session_id"), str)
            or not signed_evidence["session_id"].strip(),
            signed_evidence.get("action_verified") is not True,
            summary.get("mode") != "platform_platform_stop_verified",
            summary.get("authenticity") != "ed25519-verified",
            summary.get("cryptographic_verification") != "passed",
            summary.get("contract_validation") != "passed",
            summary.get("key_id") != key_id,
            summary.get("signature_algorithm") != "ed25519",
            summary.get("evidence_sha256") != hashlib.sha256(_canonical_json(signed_evidence)).hexdigest(),
            summary.get("proof_sha256") != signed_evidence.get("proof_sha256"),
            not isinstance(signed_evidence.get("proof_sha256"), str)
            or not SHA256_RE.fullmatch(signed_evidence["proof_sha256"]),
        )):
            raise _error("stop receipt summary differs from its signed attestation", E_ISOLATION_UNVERIFIED)
        stopped_at = _parse_attestation_time(signed_evidence.get("stopped_at"), "stopped_at")
        receipt_time = _parse_attestation_time(receipt_at, "receipt.at")
        validation_time = datetime.now(timezone.utc)
        latest_allowed = validation_time + MAX_ATTESTATION_CLOCK_SKEW
        if (
            stopped_at.astimezone(timezone.utc) > latest_allowed
            or receipt_time <= stopped_at
            or receipt_time.astimezone(timezone.utc) > validation_time
        ):
            raise _error("stop receipt time is outside the signed stop/current-time window", E_ISOLATION_UNVERIFIED)
        for field in ("mechanism", "target", "proof_reference"):
            _require_string(signed_evidence, field)
            if summary.get(field) != signed_evidence.get(field):
                raise _error("stop receipt summary field %s mismatch" % field, E_ISOLATION_UNVERIFIED)
        _validate_stop_proof_reference(signed_evidence.get("proof_reference"))
        if signed_evidence.get("platform_id", "").casefold() == "openclaw":
            if (
                not isinstance(signed_evidence.get("automation_job_id"), str)
                or not signed_evidence["automation_job_id"].strip()
                or signed_evidence.get("removal_verified") is not True
                or summary.get("automation_job_id") != signed_evidence.get("automation_job_id")
                or summary.get("removal_verified") is not True
                or summary.get("removal_checked_at") != signed_evidence.get("removal_checked_at")
            ):
                raise _error("OpenClaw stop receipt lacks verified automation removal", E_ISOLATION_UNVERIFIED)
            removal_checked_at = _parse_attestation_time(signed_evidence.get("removal_checked_at"), "removal_checked_at")
            if (
                removal_checked_at < stopped_at
                or removal_checked_at.astimezone(timezone.utc) > latest_allowed
                or receipt_time <= removal_checked_at
            ):
                raise _error("OpenClaw receipt time is before or inconsistent with automation removal", E_ISOLATION_UNVERIFIED)
        return
    if (
        summary.get("mode") != "restricted_view"
        or summary.get("authenticity") != "path_and_hash_contract_only"
        or summary.get("view_root") != view_root
        or summary.get("allowed_read_roots") != expected_read_roots
        or summary.get("allowed_write_roots") != expected_write_roots
        or not SHA256_RE.fullmatch(str(summary.get("scope_digest", "")))
        or not SHA256_RE.fullmatch(str(summary.get("input_manifest_sha256", "")))
        or not all(isinstance(summary.get(field), str) and summary[field].strip() for field in (
            "view_root", "scope_digest", "input_manifest_sha256",
        ))
        or not isinstance(summary.get("allowed_read_roots"), list)
        or not isinstance(summary.get("allowed_write_roots"), list)
    ):
        raise _error("receipt requires a validated restricted-view summary", E_ISOLATION_UNVERIFIED)


@dataclass(frozen=True)
class RuntimeManifest:
    runtime_version: str
    protocol_version: str
    entrypoint: str
    files: tuple[dict[str, str], ...]
    minimum_python: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeManifest":
        required = ("runtime_version", "protocol_version", "entrypoint", "files", "minimum_python")
        for field in required:
            if field not in value:
                raise _error("missing manifest field %s" % field, E_SCHEMA, field=field)
        files = value["files"]
        if not isinstance(files, list) or not files:
            raise _error("manifest files must be a non-empty list", E_SCHEMA)
        checked: list[dict[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                raise _error("invalid manifest file entry", E_SCHEMA)
            checked.append({"path": item["path"], "sha256": item["sha256"]})
        return cls(_require_string(value, "runtime_version"), _require_string(value, "protocol_version"), _require_string(value, "entrypoint"), tuple(checked), _require_string(value, "minimum_python"))

    def to_dict(self) -> dict[str, Any]:
        return {"runtime_version": self.runtime_version, "protocol_version": self.protocol_version, "entrypoint": self.entrypoint, "files": list(self.files), "minimum_python": self.minimum_python}


def _runtime_root(workspace: Path, version: str = RUNTIME_VERSION) -> Path:
    return path_in_workspace(workspace, ".multiagent/runtime/participant/%s" % version)


def _source_runtime_files() -> tuple[Path, ...]:
    package = Path(__file__).resolve().parent
    return (
        package / "__init__.py", package / "protocol.py", package / "participant_runner.py",
        package / "runtime_core.py", package.parent / "attestation_keys.py",
    )


def publish_runtime(workspace: Path, runtime_version: str = RUNTIME_VERSION) -> RuntimeManifest:
    """Atomically publish a versioned Runtime under the workspace.

    A published version is immutable. Repeating publication validates and returns
    the existing matching manifest; a conflicting existing version is rejected.
    """
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    target = _runtime_root(workspace, runtime_version)
    manifest_path = target / "manifest.json"
    if manifest_path.exists():
        manifest = RuntimeManifest.from_dict(load_json(manifest_path))
        verify_manifest(workspace, manifest)
        return manifest
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=".runtime-", dir=parent))
    try:
        files: list[dict[str, str]] = []
        for source in _source_runtime_files():
            if not source.is_file():
                raise _error("runtime source is missing", E_RUNTIME_VERSION, source=str(source))
            destination = temp_root / source.name
            shutil.copyfile(source, destination)
            files.append({"path": ".multiagent/runtime/participant/%s/%s" % (runtime_version, source.name), "sha256": sha256_file(destination)})
        template_dir = temp_root / "templates"
        template_dir.mkdir()
        bootstrap = template_dir / "bootstrap-instruction.json"
        atomic_write_json(bootstrap, {"kind": "bootstrap", "message": "Validate the project-scoped Participant Runtime, then write an accepted receipt."})
        files.append({"path": ".multiagent/runtime/participant/%s/templates/bootstrap-instruction.json" % runtime_version, "sha256": sha256_file(bootstrap)})
        manifest = RuntimeManifest(runtime_version, PROTOCOL_VERSION, ".multiagent/runtime/participant/%s/participant_runner.py" % runtime_version, tuple(files), "3.11")
        atomic_write_json(temp_root / "manifest.json", manifest.to_dict())
        if target.exists():
            raise _error("runtime version already exists", E_STATE_CONFLICT, runtime_version=runtime_version)
        os.replace(temp_root, target)
        return manifest
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def verify_manifest(
    workspace: Path,
    manifest: RuntimeManifest | None = None,
    runtime_version: str | None = None,
) -> RuntimeManifest:
    workspace = Path(workspace).resolve()
    if manifest is None:
        if runtime_version is not None:
            manifest_path = path_in_workspace(
                workspace,
                ".multiagent/runtime/participant/%s/manifest.json" % runtime_version,
            )
            if not manifest_path.is_file():
                raise _error(
                    "requested runtime manifest does not exist",
                    E_RUNTIME_VERSION,
                    runtime_version=runtime_version,
                )
            manifest = RuntimeManifest.from_dict(load_json(manifest_path))
        else:
            versions = sorted((workspace / ".multiagent" / "runtime" / "participant").glob("*/manifest.json"))
            if len(versions) != 1:
                raise _error("exactly one runtime manifest must be selected", E_RUNTIME_VERSION)
            manifest = RuntimeManifest.from_dict(load_json(versions[0]))
    if runtime_version is not None and manifest.runtime_version != runtime_version:
        raise _error(
            "selected manifest version differs from requested runtime",
            E_RUNTIME_VERSION,
            requested=runtime_version,
            actual=manifest.runtime_version,
        )
    for item in manifest.files:
        path = path_in_workspace(workspace, item["path"])
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise _error("runtime manifest checksum mismatch", E_HASH, path=item["path"])
    return manifest


def instruction_path(workspace: Path, agent_id: str, instruction: Instruction) -> Path:
    _validate_agent_id(agent_id)
    validate_instruction_id(instruction.instruction_id)
    return path_in_workspace(Path(workspace), ".multiagent/instructions/%s/%06d-%s-%s.json" % (agent_id, instruction.sequence, instruction.kind, instruction.instruction_id))


def write_instruction(workspace: Path, instruction: Instruction) -> Path:
    if instruction.agent_id != _validate_agent_id(instruction.agent_id):
        raise _error("instruction agent identity invalid", E_SCHEMA)
    # Validate the public serialized form here as well.  Callers may construct
    # the frozen dataclass directly, so parsing only on read would leave a
    # bypass around the OpenClaw mandatory-directive and checksum boundaries.
    Instruction.from_dict(instruction.to_dict())
    path = instruction_path(workspace, instruction.agent_id, instruction)
    if path.exists():
        existing = Instruction.from_dict(load_json(path))
        if existing.to_dict() != instruction.to_dict():
            raise _error("published instruction is immutable", E_STATE_CONFLICT, instruction_id=instruction.instruction_id)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, instruction.to_dict())
    return path


def receipt_path(workspace: Path, agent_id: str, instruction_id: str, status: str) -> Path:
    _validate_agent_id(agent_id)
    validate_instruction_id(instruction_id)
    if status not in {"accepted", "completed", "failed"}:
        raise _error("invalid receipt status", E_SCHEMA)
    return path_in_workspace(Path(workspace), ".multiagent/receipts/%s/%s-%s.json" % (agent_id, instruction_id, status))


def write_receipt(workspace: Path, agent_id: str, value: dict[str, Any]) -> Literal["written", "idempotent"]:
    receipt = Receipt.from_dict(value)
    if receipt.agent_id != _validate_agent_id(agent_id):
        raise _error("receipt agent differs from write scope", E_PATH_SCOPE, agent_id=agent_id)
    path = receipt_path(workspace, agent_id, receipt.instruction_id, receipt.status)
    terminal_paths = [receipt_path(workspace, agent_id, receipt.instruction_id, status) for status in TERMINAL_STATUSES]
    if receipt.status in TERMINAL_STATUSES:
        for other in terminal_paths:
            if other.exists():
                existing = Receipt.from_dict(load_json(other))
                if existing.payload == receipt.payload and other == path:
                    return "idempotent"
                raise _error("a conflicting terminal receipt already exists", E_STATE_CONFLICT, instruction_id=receipt.instruction_id)
    if path.exists():
        if Receipt.from_dict(load_json(path)).payload == receipt.payload:
            return "idempotent"
        raise _error("receipt is append-only", E_STATE_CONFLICT, instruction_id=receipt.instruction_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, receipt.payload)
    return "written"
