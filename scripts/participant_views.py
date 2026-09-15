#!/usr/bin/env python3
"""生成参与者密封输入视图；平台必须把会话权限限制在该视图。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from attestation_keys import configured_verifier_binding  # type: ignore[import-not-found]

from workflow_core import (
    E_PATH_SCOPE,
    E_STATE_CONFLICT,
    WorkflowError,
    atomic_write_json,
    path_in_workspace,
    sha256_file,
    validate_participant_id,
)
from participant_runtime.protocol import InputManifest, input_manifest_path, input_scope_digest


INDEPENDENCE_NOTE = (
    "A sealed view is a path-scoped capability boundary, not proof of hard isolation. "
    "Do not claim independence without platform sandbox/allowlist execution evidence; "
    "ordinary ACLs under the same user are not hard isolation."
)


def view_root(workspace: Path, agent_id: str) -> Path:
    agent_id = validate_participant_id(agent_id)
    return path_in_workspace(workspace, ".multiagent/views/%s" % agent_id)


def _publish_manifest(workspace: Path, agent_id: str, published: list[str]) -> None:
    view_relative = ".multiagent/views/%s" % agent_id
    files = [
        {"path": relative, "sha256": sha256_file(path_in_workspace(workspace, relative))}
        for relative in published
    ]
    manifest = InputManifest(
        agent_id=agent_id,
        view_root=view_relative,
        files=tuple((item["path"], item["sha256"]) for item in files),
        scope_digest=input_scope_digest(agent_id, files),
    )
    data = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_sha256 = hashlib.sha256(data).hexdigest()
    manifest_relative = input_manifest_path(agent_id, manifest_sha256)
    manifest_target = path_in_workspace(workspace, manifest_relative)
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    if manifest_target.exists():
        if not manifest_target.is_file() or manifest_target.read_bytes() != data:
            raise WorkflowError("sealed input manifest is immutable", E_STATE_CONFLICT, path=manifest_relative)
    else:
        manifest_target.write_bytes(data)
    atomic_write_json(
        path_in_workspace(workspace, view_relative + "/current_input_manifest.json"),
        {
            "path": manifest_relative,
            "sha256": manifest_sha256,
            "scope_digest": manifest.scope_digest,
        },
    )


def publish_inputs(
    workspace: Path,
    agent_id: str,
    sources: list[tuple[Path, str]],
    *,
    instruction_kind: str = "propose",
) -> list[str]:
    workspace = Path(workspace).resolve()
    agent_id = validate_participant_id(agent_id)
    supported_kinds = {"bootstrap", "propose", "respond", "repair", "upgrade", "stop"}
    if instruction_kind not in supported_kinds:
        raise WorkflowError("unsupported instruction kind", E_PATH_SCOPE, kind=instruction_kind)
    input_root = view_root(workspace, agent_id) / "inputs"
    input_root.mkdir(parents=True, exist_ok=True)
    published: list[str] = []
    seen_names: set[str] = set()
    for source, name in sources:
        source_path = Path(source).resolve()
        try:
            source_path.relative_to(workspace)
        except ValueError as exc:
            raise WorkflowError("input source is outside the workspace", E_PATH_SCOPE, path=str(source)) from exc
        if not source_path.is_file():
            raise WorkflowError("input source is not a file", E_PATH_SCOPE, path=str(source))
        source_relative = source_path.relative_to(workspace).as_posix()
        allowed_source = source_relative == "project-context.md"
        if instruction_kind == "bootstrap":
            allowed_source = allowed_source or (
                source_relative.startswith(".multiagent/runtime/participant/")
                and source_relative.endswith("/manifest.json")
            )
        elif instruction_kind == "respond":
            state_path = workspace / ".multiagent" / "state.json"
            discussion_relative = None
            if state_path.is_file():
                try:
                    state_value = json.loads(state_path.read_text(encoding="utf-8"))
                    discussion_relative = (state_value.get("content_authority") or {}).get("discussion_path")
                except (OSError, json.JSONDecodeError):
                    discussion_relative = None
            is_main_discussion = (
                source_relative == discussion_relative
                if isinstance(discussion_relative, str)
                else source_path.parent == workspace and source_path.suffix.lower() == ".md"
            )
            allowed_source = allowed_source or (is_main_discussion and name == "discussion.md")
        elif instruction_kind == "repair":
            own_view_outputs = ".multiagent/views/%s/outputs/" % agent_id
            own_receipts = ".multiagent/receipts/%s/" % agent_id
            allowed_source = allowed_source or source_relative.startswith(own_view_outputs) or source_relative.startswith(own_receipts)
        if not allowed_source:
            raise WorkflowError(
                "input source is not allowed for this instruction phase",
                E_PATH_SCOPE,
                path=source_relative,
                kind=instruction_kind,
                agent_id=agent_id,
            )
        target_relative = Path(".multiagent") / "views" / agent_id / "inputs" / name
        target = path_in_workspace(workspace, target_relative)
        try:
            target.relative_to(input_root)
        except ValueError as exc:
            raise WorkflowError("input target is outside the participant view", E_PATH_SCOPE, path=name) from exc
        source_content = source_path.read_bytes()
        if target.exists() and target.read_bytes() != source_content:
            requested_name = Path(name)
            digest_suffix = hashlib.sha256(source_content).hexdigest()[:12]
            versioned_name = requested_name.with_name(
                "%s-%s%s" % (requested_name.stem, digest_suffix, requested_name.suffix)
            )
            target = path_in_workspace(
                workspace,
                Path(".multiagent") / "views" / agent_id / "inputs" / versioned_name,
            )
        try:
            target.relative_to(input_root)
        except ValueError as exc:
            raise WorkflowError("input target is outside the participant view", E_PATH_SCOPE, path=name) from exc
        relative = target.relative_to(workspace).as_posix()
        if relative in seen_names:
            raise WorkflowError("duplicate input target", E_PATH_SCOPE, path=relative)
        seen_names.add(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or target.read_bytes() != source_content:
                raise WorkflowError("sealed input snapshot is immutable", E_STATE_CONFLICT, path=relative)
        else:
            target.write_bytes(source_content)
        published.append(relative)
    _publish_manifest(workspace, agent_id, published)
    return published


def output_path(workspace: Path, agent_id: str, kind: str) -> str:
    workspace = Path(workspace).resolve()
    root = view_root(workspace, agent_id) / "outputs"
    root.mkdir(parents=True, exist_ok=True)
    if kind in {"propose", "repair"}:
        name = "提案文档.md"
    elif kind == "respond":
        name = "交叉回应文档.md"
    else:
        if kind not in {"bootstrap", "upgrade", "stop"}:
            raise WorkflowError("unsupported instruction kind", E_PATH_SCOPE, kind=kind)
        return ".multiagent/receipts/%s" % validate_participant_id(agent_id)
    return (root / name).relative_to(workspace).as_posix()


def access_scope(workspace: Path, agent_id: str) -> dict[str, object]:
    workspace = Path(workspace).resolve()
    agent_id = validate_participant_id(agent_id)
    root = view_root(workspace, agent_id).relative_to(workspace).as_posix()
    current_manifest_path = path_in_workspace(
        workspace,
        root + "/current_input_manifest.json",
    )
    if not current_manifest_path.is_file():
        raise WorkflowError("participant view has no published input manifest", E_STATE_CONFLICT, agent_id=agent_id)
    try:
        reference = json.loads(current_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("participant input manifest reference is unreadable", E_STATE_CONFLICT, agent_id=agent_id) from exc
    manifest_relative = input_manifest_path(agent_id, str(reference.get("sha256", "")))
    if reference.get("path") != manifest_relative:
        raise WorkflowError("participant input manifest reference is invalid", E_STATE_CONFLICT, agent_id=agent_id)
    manifest_file = path_in_workspace(workspace, manifest_relative)
    if not manifest_file.is_file() or sha256_file(manifest_file) != reference.get("sha256"):
        raise WorkflowError("participant input manifest reference hash mismatch", E_STATE_CONFLICT, agent_id=agent_id)
    try:
        manifest = InputManifest.from_dict(json.loads(manifest_file.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("participant input manifest is unreadable", E_STATE_CONFLICT, agent_id=agent_id) from exc
    if manifest.scope_digest != reference.get("scope_digest"):
        raise WorkflowError("participant input manifest scope digest mismatch", E_STATE_CONFLICT, agent_id=agent_id)
    scope: dict[str, object] = {
        "mode": "sealed_view",
        "view_root": root,
        "allowed_read_roots": [root + "/inputs"],
        "allowed_write_roots": [root + "/outputs", ".multiagent/receipts/%s" % agent_id],
        "requires_platform_enforcement": True,
        "independence_claim_requires_enforcement_receipt": True,
        "security_note": INDEPENDENCE_NOTE,
        "input_manifest_path": manifest_relative,
        "input_manifest_sha256": reference["sha256"],
        "scope_digest": manifest.scope_digest,
    }
    try:
        state_path = path_in_workspace(workspace, ".multiagent/state.json")
        trust_pin: dict[str, object] | None = None
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("state.json must be an object")
            raw_pin = state.get("isolation_trust")
            if raw_pin is not None:
                if not isinstance(raw_pin, dict):
                    raise ValueError("state isolation_trust must be an object")
                trust_pin = raw_pin
        verifier = configured_verifier_binding(
            key_id=str(trust_pin.get("key_id")) if trust_pin is not None else None,
            public_key_b64_value=str(trust_pin.get("public_key_b64")) if trust_pin is not None else None,
        )
    except ValueError as exc:
        raise WorkflowError("E_ISOLATION_UNVERIFIED: invalid external verifier configuration", "E_ISOLATION_UNVERIFIED") from exc
    if verifier is not None:
        scope["attestation_verifier"] = verifier
    return scope
