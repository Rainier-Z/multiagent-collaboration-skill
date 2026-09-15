#!/usr/bin/env python3
"""Tests for trusted platform stop adapters using injected command runners."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from adapters.openclaw.stop_adapter import (  # noqa: E402
    CommandResult,
    OpenClawStopError,
    stop_openclaw_automation,
)
from attestation_keys import verify_payload_signature  # noqa: E402
from participant_runtime.protocol import Instruction, load_stop_attestation  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


class OpenClawStopAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="openclaw-stop-adapter-")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.private_key_path = self.root / "external" / "platform-private.pem"
        self.private_key_path.parent.mkdir()
        private_key = Ed25519PrivateKey.generate()
        self.private_key_path.write_bytes(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        self.public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )).decode("ascii")
        self.key_id = "openclaw-test-key"
        self.instruction_id = "I-stop-openclaw-1"
        self.job_id = "cron-job-123"
        self._write_stop_context()
        self.original_trust_registry = os.environ.get("MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON")
        os.environ["MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON"] = json.dumps({self.key_id: self.public_key_b64})

    def tearDown(self) -> None:
        if self.original_trust_registry is None:
            os.environ.pop("MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON", None)
        else:
            os.environ["MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON"] = self.original_trust_registry
        self.temp.cleanup()

    def _write_stop_context(self) -> None:
        metadata = self.workspace / ".multiagent"
        (metadata / "instructions" / "openclaw").mkdir(parents=True)
        (metadata / "receipts" / "openclaw").mkdir(parents=True)
        (metadata / "state.json").write_text(
            json.dumps({"discussion_id": "discussion-stop-test"}), encoding="utf-8",
        )
        (metadata / "receipts" / "openclaw" / "automation-job.md").write_text(
            "job_id: %s\nname: multiagent-openclaw-test\n" % self.job_id,
            encoding="utf-8",
        )
        manifest_sha256 = "a" * 64
        access_scope = {
            "mode": "sealed_view",
            "view_root": ".multiagent/views/openclaw",
            "allowed_read_roots": [".multiagent/views/openclaw/inputs"],
            "allowed_write_roots": [".multiagent/views/openclaw/outputs", ".multiagent/receipts/openclaw"],
            "requires_platform_enforcement": True,
            "independence_claim_requires_enforcement_receipt": True,
            "security_note": "Tests exercise only the trusted platform-side stop action.",
            "input_manifest_path": ".multiagent/views/openclaw/inputs/.manifests/%s.json" % manifest_sha256,
            "input_manifest_sha256": manifest_sha256,
            "scope_digest": "b" * 64,
            "attestation_verifier": {
                "key_id": self.key_id,
                "algorithm": "ed25519",
                "public_key_b64": self.public_key_b64,
            },
        }
        body = {
            "instruction_id": self.instruction_id,
            "discussion_id": "discussion-stop-test",
            "sequence": 1,
            "kind": "stop",
            "agent_id": "openclaw",
            "runtime_version": "1.0.0",
            "state_revision": 1,
            "task_prompt": (
                "Stop only the saved OpenClaw automation for agent openclaw. "
                "Use the trusted adapter to verify cron removal and preserve the stop evidence. "
                "Read .multiagent/views/openclaw/inputs/context.md and write only under .multiagent/receipts/openclaw."
            ),
            "input_paths": [".multiagent/views/openclaw/inputs/context.md"],
            "output_path": ".multiagent/receipts/openclaw",
            "access_scope": access_scope,
            "attempt": 1,
            "max_attempts": 1,
            "issued_at": "2026-09-14T10:00:00+08:00",
            "operational_directive": "Stop only the saved project automation.",
            "platform_id": "openclaw",
            "session_id": "openclaw-session-test",
        }
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body["sha256"] = hashlib.sha256(canonical).hexdigest()
        parsed = Instruction.from_dict(body)
        instruction_path = metadata / "instructions" / "openclaw" / ("000001-stop-%s.json" % self.instruction_id)
        instruction_path.write_text(json.dumps(parsed.to_dict(), ensure_ascii=False), encoding="utf-8")

    def _runner(self, removal_code: int = 0, listing: str = '{"jobs": []}'):
        commands: list[list[str]] = []

        def run(command: Sequence[str]) -> CommandResult:
            commands.append(list(command))
            if list(command[:3]) == ["openclaw", "cron", "rm"]:
                return CommandResult(removal_code, "removed" if removal_code == 0 else "", "")
            return CommandResult(0, listing, "")

        return commands, run

    def _call(self, runner, **kwargs):
        return stop_openclaw_automation(
            workspace=self.workspace,
            instruction_id=self.instruction_id,
            private_key_path=self.private_key_path,
            runner=runner,
            clock=lambda: datetime.now(timezone.utc),
            **kwargs,
        )

    def test_absent_job_creates_proof_and_bound_ed25519_attestation(self) -> None:
        commands, runner = self._runner(listing='{"jobs": [{"id": "other-job", "enabled": true}]}')
        result = self._call(runner)

        proof_path = self.workspace / result["proof_reference"]
        attestation_path = self.workspace / result["attestation_path"]
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        instruction_path = next((self.workspace / ".multiagent" / "instructions" / "openclaw").glob("*-stop-*.json"))
        instruction_sha256 = json.loads(instruction_path.read_text(encoding="utf-8"))["sha256"]
        self.assertEqual(commands, [
            ["openclaw", "cron", "rm", self.job_id],
            ["openclaw", "cron", "list", "--json"],
        ])
        self.assertEqual(proof["discussion_id"], "discussion-stop-test")
        self.assertEqual(proof["instruction_id"], self.instruction_id)
        self.assertEqual(proof["instruction_sha256"], instruction_sha256)
        self.assertEqual(proof["agent_id"], "openclaw")
        self.assertEqual(proof["platform_id"], "openclaw")
        self.assertEqual(proof["session_id"], "openclaw-session-test")
        for field in (
            "instruction_sha256", "stopped_at", "mechanism", "target", "action_verified",
            "automation_job_id", "removal_verified", "removal_checked_at",
        ):
            self.assertEqual(proof[field], attestation[field])
        self.assertEqual(proof["automation_job_id"], self.job_id)
        self.assertEqual(proof["removal_exit_code"], 0)
        self.assertEqual(proof["verification_exit_code"], 0)
        self.assertTrue(proof["action_verified"])
        self.assertTrue(proof["removal_verified"])
        self.assertEqual(proof["proof_details"]["verification_result"], "absent")
        self.assertEqual(attestation["proof_sha256"], hashlib.sha256(proof_path.read_bytes()).hexdigest())
        self.assertEqual(attestation["proof_reference"], result["proof_reference"])
        self.assertEqual(attestation["instruction_sha256"], instruction_sha256)
        self.assertTrue(attestation["action_verified"])
        self.assertTrue(attestation["removal_verified"])
        self.assertEqual(attestation["automation_job_id"], self.job_id)
        verify_payload_signature(
            attestation,
            expected_key_id=self.key_id,
            expected_public_key_b64=self.public_key_b64,
        )
        instruction = Instruction.from_dict(json.loads(instruction_path.read_text(encoding="utf-8")))
        summary = load_stop_attestation(self.workspace, instruction)
        self.assertEqual(summary["signed_evidence"], attestation)

    def test_disabled_present_job_never_writes_a_signed_success(self) -> None:
        commands, runner = self._runner(listing='{"jobs": [{"id": "cron-job-123", "enabled": false}]}')
        with self.assertRaisesRegex(OpenClawStopError, "still exists"):
            self._call(runner)
        self.assertEqual(commands, [
            ["openclaw", "cron", "rm", self.job_id],
            ["openclaw", "cron", "list", "--json"],
        ])
        audit = self.workspace / ".multiagent" / "audit"
        self.assertFalse(list(audit.rglob("*-stop.json")))
        self.assertFalse(list(audit.rglob("*-removal-proof.json")))

    def test_paused_or_stopped_present_job_never_writes_a_signed_success(self) -> None:
        for index, state in enumerate(("paused", "stopped", "disabled")):
            with self.subTest(state=state):
                fresh = self.root / ("workspace-present-%s" % index)
                fresh.mkdir()
                original = self.workspace
                self.workspace = fresh
                self._write_stop_context()
                commands, runner = self._runner(listing=json.dumps({"jobs": [{"id": self.job_id, "status": state}]}))
                try:
                    with self.assertRaisesRegex(OpenClawStopError, "still exists"):
                        self._call(runner)
                    self.assertEqual(len(commands), 2)
                    audit = fresh / ".multiagent" / "audit"
                    self.assertFalse(list(audit.rglob("*-stop.json")))
                    self.assertFalse(list(audit.rglob("*-removal-proof.json")))
                finally:
                    self.workspace = original

    def test_nonzero_removal_never_writes_a_signed_success(self) -> None:
        commands, runner = self._runner(
            removal_code=1,
            listing='{"jobs": []}',
        )
        with self.assertRaisesRegex(OpenClawStopError, "non-zero"):
            self._call(runner)
        self.assertEqual(len(commands), 2)
        self.assertFalse(list((self.workspace / ".multiagent" / "audit").rglob("*-stop.json")))
        self.assertFalse(list((self.workspace / ".multiagent" / "audit").rglob("*-removal-proof.json")))

    def test_enabled_or_ambiguous_native_state_never_signs(self) -> None:
        for index, listing in enumerate((
            '{"jobs": [{"id": "cron-job-123", "enabled": true}]}',
            "not-json",
            '{"unexpected": []}',
            '{"jobs": [], "items": {}}',
            '{"jobs": [{"id": 123}]}',
            '{"jobs": [], "jobs": [{"id": "cron-job-123"}]}',
            '{"jobs": [], "metadata": NaN}',
        )):
            with self.subTest(listing=listing):
                fresh = self.root / ("workspace-%s" % index)
                fresh.mkdir()
                original = self.workspace
                self.workspace = fresh
                self._write_stop_context()
                commands, runner = self._runner(listing=listing)
                try:
                    with self.assertRaises(OpenClawStopError):
                        self._call(runner)
                    self.assertEqual(len(commands), 2)
                    self.assertFalse(list((fresh / ".multiagent" / "audit").rglob("*-stop.json")))
                    self.assertFalse(list((fresh / ".multiagent" / "audit").rglob("*-removal-proof.json")))
                finally:
                    self.workspace = original

    def test_conflicting_recognized_job_sources_never_sign(self) -> None:
        listings = (
            '{"jobs": [], "items": [{"id": "cron-job-123"}]}',
            '{"jobs": [], "data": {"jobs": [{"id": "cron-job-123"}]}}',
            '{"crons": [], "items": [{"id": "cron-job-123"}]}',
        )
        for index, listing in enumerate(listings):
            with self.subTest(listing=listing):
                fresh = self.root / ("workspace-conflicting-sources-%s" % index)
                fresh.mkdir()
                original = self.workspace
                self.workspace = fresh
                self._write_stop_context()
                commands, runner = self._runner(listing=listing)
                try:
                    with self.assertRaisesRegex(OpenClawStopError, "inconsistent"):
                        self._call(runner)
                    self.assertEqual(len(commands), 2)
                    audit = fresh / ".multiagent" / "audit"
                    self.assertFalse(list(audit.rglob("*-stop.json")))
                    self.assertFalse(list(audit.rglob("*-removal-proof.json")))
                finally:
                    self.workspace = original

    def test_consistent_multiple_job_sources_are_normalized_without_duplicate_false_positive(self) -> None:
        listing = json.dumps({
            "jobs": [{"id": "other-job", "enabled": True}],
            "data": {"items": [{"job_id": "other-job", "enabled": False}]},
        })
        commands, runner = self._runner(listing=listing)

        result = self._call(runner)

        proof = json.loads((self.workspace / result["proof_reference"]).read_text(encoding="utf-8"))
        self.assertEqual(len(commands), 2)
        self.assertEqual(proof["proof_details"]["verification_result"], "absent")
        self.assertEqual(proof["proof_details"]["matching_job_count"], 0)

    def test_job_id_argument_cannot_override_saved_project_job(self) -> None:
        commands, runner = self._runner()
        with self.assertRaisesRegex(OpenClawStopError, "does not match"):
            self._call(runner, automation_job_id="another-project-job")
        self.assertEqual(commands, [])

    def test_discussion_binding_and_workspace_internal_key_fail_before_removal(self) -> None:
        commands, runner = self._runner()
        state_path = self.workspace / ".multiagent" / "state.json"
        state_path.write_text(json.dumps({"discussion_id": "different-discussion"}), encoding="utf-8")
        with self.assertRaisesRegex(OpenClawStopError, "discussion_id"):
            self._call(runner)
        self.assertEqual(commands, [])

        state_path.write_text(json.dumps({"discussion_id": "discussion-stop-test"}), encoding="utf-8")
        internal_key = self.workspace / "private.pem"
        internal_key.write_bytes(self.private_key_path.read_bytes())
        with self.assertRaisesRegex(OpenClawStopError, "outside the workspace"):
            stop_openclaw_automation(
                workspace=self.workspace,
                instruction_id=self.instruction_id,
                private_key_path=internal_key,
                runner=runner,
            )
        self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main()
