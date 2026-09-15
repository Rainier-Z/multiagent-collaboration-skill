#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security gates for coordinator identity, sealed inputs, and isolation evidence."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import workflow_core  # type: ignore[import-not-found]
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from attestation_keys import provision_key, sign_payload  # type: ignore[import-not-found]
from adapters.common.wake_protocol import WakeRequest, WakeResult  # type: ignore[import-not-found]
from adapters.claude.wake_adapter import ClaudeCodeWakeAdapter  # type: ignore[import-not-found]
from adapters.codex.wake_adapter import CodexWakeAdapter  # type: ignore[import-not-found]
from adapters.openclaw.wake_adapter import OpenClawWakeAdapter  # type: ignore[import-not-found]
from participant_views import access_scope, output_path, publish_inputs  # type: ignore[import-not-found]
from instruction_prompts import build_task_prompt  # type: ignore[import-not-found]
from participant_runtime.participant_runner import run_instruction  # type: ignore[import-not-found]
from participant_runtime.protocol import (  # type: ignore[import-not-found]
    Instruction,
    Receipt,
    _is_resolved_path_within,
    isolation_evidence_path,
    load_stop_attestation,
    publish_runtime,
    stop_attestation_path,
    validate_isolation_evidence,
    write_instruction,
)


class CoordinatorExecutionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "protocol_version": "1.0",
            "discussion_id": "security-gate-test",
            "stage": "initialized",
            "expected_participants": ["claude-a", "codex-a"],
            "submission_status": {"claude-a": "pending", "codex-a": "pending"},
            "response_status": {"claude-a": "pending", "codex-a": "pending"},
            "coordinator": "claude-a",
            "coordinator_binding": {
                "agent_id": "claude-a",
                "role": "coordinator",
                "platform_id": "claude-code",
                "session_id": "session-actual-1",
            },
            "revision": 1,
        }

    def test_coordinator_execution_requires_an_exact_four_field_binding(self) -> None:
        self.assertTrue(hasattr(workflow_core, "validate_coordinator_execution"))
        validate = getattr(workflow_core, "validate_coordinator_execution")
        self.assertIsNone(validate(self.state, "claude-a", "claude-code", "session-actual-1"))

        for values in (
            ("codex-a", "claude-code", "session-actual-1"),
            ("claude-a", "codex", "session-actual-1"),
            ("claude-a", "claude-code", "session-other"),
        ):
            with self.subTest(values=values), self.assertRaises(workflow_core.WorkflowError) as caught:
                validate(self.state, *values)
            self.assertEqual(caught.exception.code, workflow_core.E_COORDINATOR_BINDING)


class WakeRequestTargetTests(unittest.TestCase):
    def test_wake_request_carries_target_platform_and_session_through_adapter(self) -> None:
        self.assertIn("platform_id", WakeRequest.__dataclass_fields__)
        self.assertIn("session_id", WakeRequest.__dataclass_fields__)
        request = WakeRequest(
            workspace=Path("workspace"),
            agent_id="claude-a",
            instruction_id="I-0001",
            runtime_version="1.0.0",
            platform_id="claude-code",
            session_id="participant-session-7",
        )
        self.assertEqual(request.to_dict()["platform_id"], "claude-code")
        self.assertEqual(request.to_dict()["session_id"], "participant-session-7")

        for adapter_type in (ClaudeCodeWakeAdapter, CodexWakeAdapter, OpenClawWakeAdapter):
            received: list[WakeRequest] = []
            adapter = adapter_type(lambda item: (received.append(item) or WakeResult("accepted")))
            with self.subTest(adapter=adapter_type.__name__):
                self.assertEqual(adapter.wake(request).status, "accepted")
                self.assertEqual(received, [request])
        self.assertIn('"platform_id":"claude-code"', request.to_json())
        self.assertIn('"session_id":"participant-session-7"', request.to_json())


class SealedInputManifestTests(unittest.TestCase):
    def test_published_view_has_an_immutable_hashed_input_manifest(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="sealed-manifest-test-"))
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        source = workspace / "project-context.md"
        source.write_text("# Initial context\n", encoding="utf-8")

        published = publish_inputs(workspace, "claude-a", [(source, "project-context.md")])
        scope = access_scope(workspace, "claude-a")

        self.assertTrue(scope.get("input_manifest_path"))
        self.assertTrue(scope.get("input_manifest_sha256"))
        self.assertTrue(scope.get("scope_digest"))
        manifest_path = workspace / str(scope["input_manifest_path"])
        manifest_bytes = manifest_path.read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), scope["input_manifest_sha256"])
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest["files"], [{
            "path": published[0],
            "sha256": hashlib.sha256((workspace / published[0]).read_bytes()).hexdigest(),
        }])
        self.assertEqual(manifest["scope_digest"], scope["scope_digest"])

        publish_inputs(workspace, "claude-a", [(source, "project-context.md")])
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)


class ParticipantRunnerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="participant-security-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.workspace, ignore_errors=True))
        publish_runtime(self.workspace)
        self.context = self.workspace / "project-context.md"
        self.context.write_text("# Context\n", encoding="utf-8")
        self.key_root = Path(tempfile.mkdtemp(prefix="external-attestation-key-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.key_root, ignore_errors=True))
        self.private_key = Ed25519PrivateKey.generate()
        self.key_id = "test-platform-key"
        self.private_key_path = self.key_root / "platform-private.pem"
        self.private_key_path.write_bytes(self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        public_b64 = base64.b64encode(self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )).decode("ascii")
        self.public_b64 = public_b64
        self.enterContext(patch.dict(os.environ, {
            "MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON": json.dumps({self.key_id: public_b64}),
            "MULTIAGENT_ATTESTATION_KEY_ID": self.key_id,
            "MULTIAGENT_ATTESTATION_PUBLIC_KEY_B64": public_b64,
        }))

    def _instruction(self, kind: str, instruction_id: str, platform_id: str = "claude-code") -> tuple[Instruction, Path]:
        if kind == "respond":
            source = self.workspace / "discussion.md"
            source.write_text("# Discussion\n", encoding="utf-8")
            input_paths = publish_inputs(
                self.workspace,
                "claude-a",
                [(source, "discussion.md")],
                instruction_kind="respond",
            )
        else:
            input_paths = publish_inputs(
                self.workspace,
                "claude-a",
                [(self.context, "project-context.md")],
                instruction_kind=kind,
            )
        target = output_path(self.workspace, "claude-a", kind)
        payload: dict[str, object] = {
            "instruction_id": instruction_id,
            "sequence": 1,
            "kind": kind,
            "agent_id": "claude-a",
            "runtime_version": "1.0.0",
            "state_revision": 1,
            "task_prompt": build_task_prompt(kind, "claude-a", "coordinator", input_paths, target),
            "input_paths": input_paths,
            "output_path": target,
            "access_scope": access_scope(self.workspace, "claude-a"),
            "attempt": 1,
            "max_attempts": 3,
            "discussion_id": "security-gate-test",
            "issued_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).astimezone().isoformat(timespec="milliseconds"),
            "platform_id": platform_id,
            "session_id": "participant-session-7",
        }
        payload["sha256"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        instruction = Instruction.from_dict(payload)
        write_instruction(self.workspace, instruction)
        return instruction, self.workspace / target

    def _platform_evidence(self, instruction: Instruction) -> dict[str, object]:
        scope = instruction.access_scope
        return sign_payload({
            "issuer": "platform-attestation",
            "source": "platform-attestation",
            "agent_id": instruction.agent_id,
            "instruction_id": instruction.instruction_id,
            "platform_id": instruction.platform_id,
            "session_id": instruction.session_id,
            "view_root": scope["view_root"],
            "scope_digest": scope["scope_digest"],
            "input_manifest_sha256": scope["input_manifest_sha256"],
            "allowed_read_roots": scope["allowed_read_roots"],
            "allowed_write_roots": scope["allowed_write_roots"],
            "evidence_type": "platform_sandbox",
            "enforcement_type": "platform_sandbox",
            "issued_at": "2026-09-13T12:00:00+08:00",
        }, private_key_path=self.private_key_path, key_id=self.key_id)

    def _platform_stop_attestation(self, instruction: Instruction, **changes: object) -> dict[str, object]:
        now = datetime.now(timezone.utc).astimezone()
        stopped_at = (now - timedelta(seconds=30)).isoformat(timespec="milliseconds")
        value: dict[str, object] = {
            "discussion_id": instruction.to_dict().get("discussion_id", "security-gate-test"),
            "agent_id": instruction.agent_id,
            "instruction_id": instruction.instruction_id,
            "instruction_sha256": instruction.sha256,
            "platform_id": instruction.platform_id,
            "session_id": instruction.session_id,
            "stopped_at": stopped_at,
            "mechanism": "session_monitor_stop",
            "action_verified": True,
            "target": "current session monitor instance",
            "proof_reference": ".multiagent/audit/platform-evidence/proofs/%s/%s.json" % (
                instruction.agent_id, instruction.instruction_id,
            ),
        }
        if instruction.platform_id == "openclaw":
            value.update({
                "automation_job_id": "job-123",
                "removal_verified": True,
                "removal_checked_at": (now - timedelta(seconds=15)).isoformat(timespec="milliseconds"),
            })
        value.update(changes)
        proof = {
            field: value[field]
            for field in (
                "discussion_id", "instruction_id", "instruction_sha256", "agent_id", "platform_id", "session_id",
                "stopped_at", "mechanism", "target", "action_verified",
            )
        }
        if instruction.platform_id == "openclaw":
            proof.update({field: value[field] for field in (
                "automation_job_id", "removal_verified", "removal_checked_at",
            )})
        proof_bytes = json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        proof_path = self.workspace / ".multiagent/audit/platform-evidence/proofs" / instruction.agent_id / (instruction.instruction_id + ".json")
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        proof_path.write_bytes(proof_bytes)
        value.setdefault("proof_sha256", hashlib.sha256(proof_bytes).hexdigest())
        return sign_payload(value, private_key_path=self.private_key_path, key_id=self.key_id)

    def test_stop_attestation_rejects_replay_bad_times_and_unbound_proof_artifacts(self) -> None:
        now = datetime.now(timezone.utc).astimezone()
        scenarios = (
            ("discussion-replay", {"discussion_id": "another-discussion"}),
            ("identity-mismatch", {"session_id": "other-session"}),
            ("instruction-sha-mismatch", {"instruction_sha256": "0" * 64}),
            ("old-stop", {
                "stopped_at": (now - timedelta(days=1)).isoformat(timespec="milliseconds"),
                "removal_checked_at": (now - timedelta(days=1) + timedelta(seconds=1)).isoformat(timespec="milliseconds"),
            }),
            ("future-stop", {
                "stopped_at": (now + timedelta(hours=1)).isoformat(timespec="milliseconds"),
                "removal_checked_at": (now + timedelta(hours=1, seconds=1)).isoformat(timespec="milliseconds"),
            }),
            ("early-removal-check", {
                "removal_checked_at": (now - timedelta(seconds=60)).isoformat(timespec="milliseconds"),
            }),
            ("traversal-proof", {"proof_reference": ".multiagent/audit/platform-evidence/../../outside.json"}),
            ("uri-proof", {"proof_reference": "https://platform.example/stop.json"}),
            ("absolute-proof", {"proof_reference": "C:\\outside\\stop.json"}),
            ("wrong-proof-hash", {"proof_sha256": "0" * 64}),
        )
        for suffix, changes in scenarios:
            instruction, _ = self._instruction("stop", "I-stop-proof-" + suffix, platform_id="openclaw")
            evidence_path = stop_attestation_path(self.workspace, instruction)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(self._platform_stop_attestation(instruction, **changes)), encoding="utf-8")
            with self.subTest(case=suffix):
                try:
                    load_stop_attestation(self.workspace, instruction)
                except workflow_core.WorkflowError as exc:
                    self.assertEqual(exc.code, "E_ISOLATION_UNVERIFIED")
                else:
                    self.fail("invalid stop proof was accepted")

        instruction, _ = self._instruction("stop", "I-stop-proof-missing", platform_id="claude-code")
        evidence = self._platform_stop_attestation(instruction)
        proof_path = self.workspace / str(evidence["proof_reference"])
        proof_path.unlink()
        evidence_path = stop_attestation_path(self.workspace, instruction)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaises(workflow_core.WorkflowError) as caught:
            load_stop_attestation(self.workspace, instruction)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

        for field in (
            "discussion_id", "instruction_id", "instruction_sha256", "agent_id", "platform_id", "session_id",
            "stopped_at", "mechanism", "target", "action_verified",
            "automation_job_id", "removal_verified", "removal_checked_at",
        ):
            instruction, _ = self._instruction("stop", "I-stop-proof-field-" + field, platform_id="openclaw")
            attestation = self._platform_stop_attestation(instruction)
            proof_path = self.workspace / str(attestation["proof_reference"])
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof[field] = False if isinstance(proof.get(field), bool) else "different-value"
            proof_bytes = json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            proof_path.write_bytes(proof_bytes)
            attestation["proof_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
            resigned = sign_payload(
                {key: value for key, value in attestation.items() if key != "signature"},
                private_key_path=self.private_key_path,
                key_id=self.key_id,
            )
            stop_path = stop_attestation_path(self.workspace, instruction)
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.write_text(json.dumps(resigned), encoding="utf-8")
            with self.subTest(proof_field=field), self.assertRaises(workflow_core.WorkflowError) as caught:
                load_stop_attestation(self.workspace, instruction)
            self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_stop_receipt_time_must_follow_stop_and_removal_and_not_be_far_future(self) -> None:
        def make_receipt(platform_id: str, instruction_id: str) -> tuple[dict[str, object], dict[str, object]]:
            instruction, _ = self._instruction("stop", instruction_id, platform_id=platform_id)
            evidence = self._platform_stop_attestation(instruction)
            summary: dict[str, object] = {
                "mode": "platform_platform_stop_verified",
                "agent_id": instruction.agent_id,
                "instruction_id": instruction.instruction_id,
                "platform_id": instruction.platform_id,
                "session_id": instruction.session_id,
                "discussion_id": instruction.to_dict().get("discussion_id", "security-gate-test"),
                "instruction_sha256": instruction.sha256,
                "stopped_at": evidence["stopped_at"],
                "mechanism": evidence["mechanism"],
                "target": evidence["target"],
                "proof_reference": evidence["proof_reference"],
                "proof_sha256": evidence["proof_sha256"],
                "action_verified": True,
                "evidence_sha256": hashlib.sha256(json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
                "key_id": self.key_id,
                "signature_algorithm": "ed25519",
                "cryptographic_verification": "passed",
                "authenticity": "ed25519-verified",
                "contract_validation": "passed",
                "attestation_verifier": instruction.access_scope["attestation_verifier"],
                "signed_evidence": evidence,
            }
            if platform_id == "openclaw":
                summary.update({
                    "automation_job_id": evidence["automation_job_id"],
                    "removal_verified": evidence["removal_verified"],
                    "removal_checked_at": evidence["removal_checked_at"],
                })
            receipt: dict[str, object] = {
                "instruction_id": instruction.instruction_id,
                "kind": "stop",
                "agent_id": instruction.agent_id,
                "runtime_version": instruction.runtime_version,
                "state_revision": instruction.state_revision,
                "status": "completed",
                "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
                "attempt": 1,
                "output_path": instruction.output_path + "/marker.txt",
                "output_sha256": "0" * 64,
                "isolation_evidence": summary,
            }
            return evidence, receipt

        evidence, receipt = make_receipt("claude-code", "I-stop-receipt-time-bounds-claude")
        self.assertTrue(Receipt.from_dict(receipt))
        stopped_at = datetime.fromisoformat(str(evidence["stopped_at"]))
        for delay in (timedelta(milliseconds=1), timedelta(seconds=1)):
            valid = dict(receipt)
            valid["at"] = (stopped_at + delay).isoformat()
            with self.subTest(platform="claude-code", delay=delay):
                self.assertTrue(Receipt.from_dict(valid))
        invalid_at_stop = dict(receipt)
        invalid_at_stop["at"] = stopped_at.isoformat()
        with self.assertRaises(workflow_core.WorkflowError) as caught:
            Receipt.from_dict(invalid_at_stop)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

        evidence, receipt = make_receipt("openclaw", "I-stop-receipt-time-bounds-openclaw")
        self.assertTrue(Receipt.from_dict(receipt))
        stopped_at = datetime.fromisoformat(str(evidence["stopped_at"]))
        removal_checked_at = datetime.fromisoformat(str(evidence["removal_checked_at"]))
        rejected_receipt_times = (
            ("before-stop", stopped_at - timedelta(seconds=1)),
            ("before-removal", removal_checked_at - timedelta(seconds=1)),
            ("at-removal", removal_checked_at),
            ("one-minute-future", datetime.now(timezone.utc) + timedelta(minutes=1)),
            ("ten-years-future", datetime.now(timezone.utc) + timedelta(days=3650)),
        )
        for scenario, receipt_at in rejected_receipt_times:
            invalid = dict(receipt)
            invalid["at"] = receipt_at.isoformat()
            with self.subTest(platform="openclaw", scenario=scenario), self.assertRaises(workflow_core.WorkflowError) as caught:
                Receipt.from_dict(invalid)
            self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

        for delay in (timedelta(milliseconds=1), timedelta(seconds=1)):
            valid = dict(receipt)
            valid["at"] = (removal_checked_at + delay).isoformat()
            with self.subTest(platform="openclaw", delay=delay):
                self.assertTrue(Receipt.from_dict(valid))

    def test_stop_proof_resolved_containment_rejects_escape(self) -> None:
        evidence_root = self.workspace / ".multiagent/audit/platform-evidence"
        inside = evidence_root / "proofs/agent/proof.json"
        outside = self.workspace / ".multiagent/audit/outside-proof.json"
        self.assertTrue(_is_resolved_path_within(inside, evidence_root))
        self.assertFalse(_is_resolved_path_within(outside, evidence_root))

        instruction, _ = self._instruction("stop", "I-stop-proof-symlink-escape", platform_id="claude-code")
        attestation = self._platform_stop_attestation(instruction)
        proof_path = self.workspace / str(attestation["proof_reference"])
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(proof_path.read_bytes())
        proof_path.unlink()
        try:
            proof_path.symlink_to(outside)
        except (OSError, NotImplementedError):
            # The helper assertions above exercise the resolve/containment
            # semantics when this host cannot create a filesystem symlink.
            self.assertFalse(_is_resolved_path_within(outside, evidence_root))
            return

        stop_path = stop_attestation_path(self.workspace, instruction)
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text(json.dumps(attestation), encoding="utf-8")
        with self.assertRaises(workflow_core.WorkflowError) as caught:
            load_stop_attestation(self.workspace, instruction)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_stop_proof_hash_binds_raw_file_bytes(self) -> None:
        instruction, _ = self._instruction("stop", "I-stop-proof-raw-hash", platform_id="claude-code")
        attestation = self._platform_stop_attestation(instruction)
        proof_path = self.workspace / str(attestation["proof_reference"])
        proof_path.write_bytes(proof_path.read_bytes() + b"\n")
        stop_path = stop_attestation_path(self.workspace, instruction)
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text(json.dumps(attestation), encoding="utf-8")

        with self.assertRaises(workflow_core.WorkflowError) as caught:
            load_stop_attestation(self.workspace, instruction)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_key_provisioning_is_exclusive_and_refuses_project_internal_private_keys(self) -> None:
        with self.assertRaises(ValueError):
            provision_key(
                workspace=self.workspace,
                private_key_path=self.workspace / "private.pem",
                key_id="new-key",
            )
        external = self.key_root / "provisioned.pem"
        public = provision_key(workspace=self.workspace, private_key_path=external, key_id="provisioned")
        self.assertEqual(set(public), {"key_id", "public_key_b64", "trusted_keys_json"})
        self.assertNotIn("PRIVATE", json.dumps(public))
        self.assertTrue(external.is_file())
        with self.assertRaises(FileExistsError):
            provision_key(workspace=self.workspace, private_key_path=external, key_id="provisioned")

    def test_stop_runner_requires_fresh_bound_signed_attestation(self) -> None:
        instruction, _ = self._instruction("stop", "I-stop-openclaw", platform_id="openclaw")
        result = run_instruction(self.workspace, "claude-a", instruction.instruction_id)
        self.assertEqual(result, "E_ISOLATION_UNVERIFIED")
        receipts = self.workspace / ".multiagent/receipts/claude-a"
        self.assertFalse((receipts / "I-stop-openclaw-completed.json").exists())
        failed = json.loads((receipts / "I-stop-openclaw-failed.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["error_code"], "E_ISOLATION_UNVERIFIED")

        # Replace the failed fixture with a new instruction identity for the
        # positive and adversarial platform checks.
        for instruction_id, changes in (
            ("I-stop-openclaw-session", {"session_id": "other-session"}),
            ("I-stop-openclaw-removal", {"removal_verified": False}),
        ):
            bad_instruction, _ = self._instruction("stop", instruction_id, platform_id="openclaw")
            evidence_path = stop_attestation_path(self.workspace, bad_instruction)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(self._platform_stop_attestation(bad_instruction, **changes)), encoding="utf-8")
            self.assertEqual(run_instruction(self.workspace, "claude-a", instruction_id), "E_ISOLATION_UNVERIFIED")
            self.assertFalse((receipts / f"{instruction_id}-completed.json").exists())

        valid_instruction, _ = self._instruction("stop", "I-stop-openclaw-valid", platform_id="openclaw")
        evidence_path = stop_attestation_path(self.workspace, valid_instruction)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        valid_payload = self._platform_stop_attestation(valid_instruction)
        evidence_path.write_text(json.dumps(valid_payload), encoding="utf-8")
        summary = load_stop_attestation(self.workspace, valid_instruction)
        self.assertEqual(summary["mode"], "platform_platform_stop_verified")
        self.assertEqual(summary["authenticity"], "ed25519-verified")
        self.assertTrue(summary["removal_verified"])
        self.assertEqual(run_instruction(self.workspace, "claude-a", valid_instruction.instruction_id), 0)
        completed = json.loads((receipts / "I-stop-openclaw-valid-completed.json").read_text(encoding="utf-8"))
        valid_marker = receipts / (valid_instruction.instruction_id + "-stop-marker.txt")
        self.assertEqual(completed["isolation_evidence"], summary)
        self.assertEqual(completed["output_path"], valid_marker.relative_to(self.workspace).as_posix())
        self.assertTrue(valid_marker.is_file())
        self.assertEqual(completed["output_sha256"], hashlib.sha256(valid_marker.read_bytes()).hexdigest())
        self.assertTrue(Receipt.from_dict(completed))

    def test_stop_receipt_rejects_tampered_or_forged_openclaw_removal_claim(self) -> None:
        instruction, _ = self._instruction("stop", "I-stop-forged", platform_id="openclaw")
        evidence = self._platform_stop_attestation(instruction)
        scope = instruction.access_scope
        # Construct the summary using a valid signature, then tamper the signed
        # claim. Receipt revalidation must fail even if the local marker exists.
        evidence["removal_verified"] = False
        forged_summary = {
            "mode": "platform_platform_stop_verified",
            "agent_id": instruction.agent_id,
            "instruction_id": instruction.instruction_id,
            "platform_id": instruction.platform_id,
            "session_id": instruction.session_id,
            "stopped_at": evidence["stopped_at"],
            "mechanism": evidence["mechanism"],
            "target": evidence["target"],
            "proof_reference": evidence["proof_reference"],
            "action_verified": True,
            "evidence_sha256": "0" * 64,
            "key_id": self.key_id,
            "signature_algorithm": "ed25519",
            "cryptographic_verification": "passed",
            "authenticity": "ed25519-verified",
            "contract_validation": "passed",
            "attestation_verifier": scope["attestation_verifier"],
            "signed_evidence": evidence,
            "automation_job_id": "job-123",
            "removal_verified": True,
            "removal_checked_at": "2026-09-13T12:05:03+08:00",
        }
        receipt = {
            "instruction_id": instruction.instruction_id,
            "kind": "stop",
            "agent_id": instruction.agent_id,
            "runtime_version": instruction.runtime_version,
            "state_revision": instruction.state_revision,
            "status": "completed",
            "at": "2026-09-13T12:06:00+08:00",
            "attempt": 1,
            "output_path": instruction.output_path + "/marker.txt",
            "output_sha256": "0" * 64,
            "isolation_evidence": forged_summary,
        }
        with self.assertRaises(workflow_core.WorkflowError) as caught:
            Receipt.from_dict(receipt)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_runner_rejects_input_content_that_differs_from_its_manifest(self) -> None:
        instruction, output = self._instruction("propose", "I-input-hash")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# Proposal\n", encoding="utf-8")
        (self.workspace / instruction.input_paths[0]).write_text("# Tampered\n", encoding="utf-8")

        result = run_instruction(self.workspace, "claude-a", instruction.instruction_id)

        self.assertEqual(result, "E_HASH")
        receipts = self.workspace / ".multiagent/receipts/claude-a"
        self.assertTrue((receipts / "I-input-hash-failed.json").is_file())
        self.assertFalse((receipts / "I-input-hash-accepted.json").exists())
        self.assertFalse((receipts / "I-input-hash-completed.json").exists())

    def test_runner_blocks_proposal_without_platform_evidence_before_accepted(self) -> None:
        instruction, output = self._instruction("propose", "I-missing-evidence")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# Proposal\n", encoding="utf-8")

        result = run_instruction(self.workspace, "claude-a", instruction.instruction_id)

        self.assertEqual(result, "E_ISOLATION_UNVERIFIED")
        receipts = self.workspace / ".multiagent/receipts/claude-a"
        failed = json.loads((receipts / "I-missing-evidence-failed.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["error_code"], "E_ISOLATION_UNVERIFIED")
        self.assertFalse((receipts / "I-missing-evidence-accepted.json").exists())
        self.assertFalse((receipts / "I-missing-evidence-completed.json").exists())

    def test_runner_records_cryptographically_verified_platform_evidence(self) -> None:
        instruction, output = self._instruction("propose", "I-valid-evidence")
        self.assertEqual(instruction.access_scope["attestation_verifier"], {
            "key_id": self.key_id,
            "algorithm": "ed25519",
            "public_key_b64": self.public_b64,
        })
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# Proposal\n", encoding="utf-8")
        evidence_path = isolation_evidence_path(self.workspace, instruction)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(self._platform_evidence(instruction)), encoding="utf-8")

        result = run_instruction(self.workspace, "claude-a", instruction.instruction_id)

        self.assertEqual(result, 0)
        receipts = self.workspace / ".multiagent/receipts/claude-a"
        for status in ("accepted", "completed"):
            receipt = json.loads((receipts / f"I-valid-evidence-{status}.json").read_text(encoding="utf-8"))
            summary = receipt["isolation_evidence"]
            self.assertEqual(summary["mode"], "platform_enforced")
            self.assertEqual(summary["contract_validation"], "passed")
            self.assertEqual(summary["authenticity"], "ed25519-verified")
            self.assertEqual(summary["cryptographic_verification"], "passed")
            self.assertEqual(summary["signature_algorithm"], "ed25519")
            self.assertEqual(summary["key_id"], self.key_id)
            self.assertIn("signed_evidence", summary)
            forged_receipt = json.loads(json.dumps(receipt))
            forged_receipt["isolation_evidence"]["signed_evidence"]["session_id"] = "forged-session"
            with self.subTest(status=status), self.assertRaises(workflow_core.WorkflowError) as caught:
                Receipt.from_dict(forged_receipt)
            self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_platform_evidence_rejects_prompt_self_report_and_same_user_acl(self) -> None:
        instruction, _ = self._instruction("propose", "I-invalid-evidence")
        valid = self._platform_evidence(instruction)
        invalid_evidence = []
        prompt = dict(valid)
        prompt["source"] = "prompt"
        invalid_evidence.append(prompt)
        self_report = dict(valid)
        self_report["issuer"] = "self_report"
        invalid_evidence.append(self_report)
        acl = {**valid, "evidence_type": "same_user_acl", "enforcement_type": "same_user_acl"}
        invalid_evidence.append(acl)

        for evidence in invalid_evidence:
            evidence = sign_payload(evidence, private_key_path=self.private_key_path, key_id=self.key_id)
            with self.subTest(evidence=evidence), self.assertRaises(workflow_core.WorkflowError) as caught:
                validate_isolation_evidence(evidence, instruction)
            self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_unsigned_tampered_wrong_key_and_untrusted_evidence_all_fail_closed(self) -> None:
        instruction, _ = self._instruction("propose", "I-signature-gates")
        unsigned = self._platform_evidence(instruction)
        unsigned.pop("signature")
        tampered = self._platform_evidence(instruction)
        tampered["session_id"] = "attacker-session"
        wrong_key = Ed25519PrivateKey.generate()
        wrong_key_path = self.key_root / "wrong-private.pem"
        wrong_key_path.write_bytes(wrong_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        wrong_key_evidence = sign_payload(
            {key: value for key, value in self._platform_evidence(instruction).items() if key != "signature"},
            private_key_path=wrong_key_path,
            key_id=self.key_id,
        )
        for evidence in (unsigned, tampered, wrong_key_evidence):
            with self.subTest(signature=evidence.get("signature")), self.assertRaises(workflow_core.WorkflowError) as caught:
                validate_isolation_evidence(evidence, instruction)
            self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

        valid = self._platform_evidence(instruction)
        with patch.dict(
            os.environ, {"MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON": "{}"},
        ):
            with self.assertRaises(workflow_core.WorkflowError) as caught:
                validate_isolation_evidence(valid, instruction)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

        unpinned = instruction.access_scope.copy()
        unpinned.pop("attestation_verifier")
        unpinned_payload = instruction.to_dict()
        unpinned_payload["access_scope"] = unpinned
        unpinned_payload.pop("sha256")
        unpinned_payload["sha256"] = hashlib.sha256(
            json.dumps(unpinned_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        unpinned_instruction = Instruction.from_dict(unpinned_payload)
        with self.assertRaises(workflow_core.WorkflowError) as caught:
            validate_isolation_evidence(valid, unpinned_instruction)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")

    def test_respond_receipt_can_use_restricted_view_evidence(self) -> None:
        instruction, output = self._instruction("respond", "I-respond-limited")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# Response\n", encoding="utf-8")

        result = run_instruction(self.workspace, "claude-a", instruction.instruction_id)

        self.assertEqual(result, 0)
        receipts = self.workspace / ".multiagent/receipts/claude-a"
        completed = json.loads((receipts / "I-respond-limited-completed.json").read_text(encoding="utf-8"))
        self.assertEqual(completed["isolation_evidence"]["mode"], "restricted_view")
        self.assertEqual(completed["isolation_evidence"]["scope_digest"], instruction.access_scope["scope_digest"])

    def test_receipt_protocol_rejects_completed_proposal_without_isolation_summary(self) -> None:
        receipt = {
            "instruction_id": "I-no-evidence",
            "kind": "propose",
            "agent_id": "claude-a",
            "runtime_version": "1.0.0",
            "state_revision": 1,
            "status": "completed",
            "at": "2026-09-13T12:00:00+08:00",
            "attempt": 1,
            "output_path": ".multiagent/views/claude-a/outputs/提案文档.md",
            "output_sha256": "0" * 64,
        }
        with self.assertRaises(workflow_core.WorkflowError) as caught:
            Receipt.from_dict(receipt)
        self.assertEqual(caught.exception.code, "E_ISOLATION_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
