#!/usr/bin/env python3
"""Candidate and confirmed Word delivery lifecycle contracts."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import confirm_decision
import export_docx
from attestation_keys import sign_payload
from orchestrate_discussion import _new_instruction
from participant_runtime.protocol import Instruction, load_stop_attestation, receipt_path, write_instruction
from workflow_core import sha256_file


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CandidateDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="candidate-delivery-")
        self.workspace = Path(self.temp_dir.name)
        self.internal = self.workspace / ".multiagent"
        self.internal.mkdir()
        self.discussion = self.workspace / "delivery讨论文档_2026-09-13.md"
        self.discussion.write_text(
            "# Delivery discussion\n\n## 四、结构化决策包\n\n候选结论内容。\n",
            encoding="utf-8",
        )
        self.state_path = self.internal / "state.json"
        key_dir = tempfile.TemporaryDirectory(prefix="candidate-delivery-key-")
        self.key_dir = key_dir
        self.key_id = "candidate-delivery-test-key"
        self.private_key_path = Path(key_dir.name) / "private.pem"
        private_key = Ed25519PrivateKey.generate()
        self.private_key_path.write_bytes(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )).decode("ascii")
        self.attestation_env = patch.dict(os.environ, {
            "MULTIAGENT_ATTESTATION_KEY_ID": self.key_id,
            "MULTIAGENT_ATTESTATION_PUBLIC_KEY_B64": public_key_b64,
            "MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON": json.dumps({self.key_id: public_key_b64}),
        })
        self.attestation_env.start()
        self.state = {
            "protocol_version": "1.0",
            "discussion_id": "candidate-delivery",
            "stage": "confirmed_decision",
            "expected_participants": ["alpha", "beta"],
            "submission_status": {"alpha": "submitted", "beta": "submitted"},
            "coordinator": "alpha",
            "coordinator_timeout": 3600,
            "participant_timeout": 3600,
            "coordination_lease_until": "2026-09-14T00:00:00+08:00",
            "proposal_disposition": "archive",
            "monitoring": {"enabled": False, "mode": "periodic", "status": "stopped"},
            "candidate_decision_ids": ["C-0001"],
            "confirmed_decision_ids": [],
            "coordinator_binding": {
                "agent_id": "alpha", "role": "coordinator",
                "platform_id": "claude-code", "session_id": "alpha-session-1",
            },
            "last_checked_at": "2026-09-13T00:00:00+08:00",
            "revision": 4,
        }
        _write_json(self.state_path, self.state)

    def tearDown(self) -> None:
        self.attestation_env.stop()
        self.key_dir.cleanup()
        self.temp_dir.cleanup()

    def _prepare_confirmation_evidence(self, receipt_agents=("alpha", "beta")) -> dict[str, Path]:
        """Build real instruction-bound stop evidence for confirmation-gate tests."""
        now = datetime.now(timezone.utc).astimezone()
        state = dict(self.state)
        state.update({
            "stage": "user_confirmation",
            "revision": 4,
            "runtime_distribution": {"version": "1.0.0"},
            "participant_bindings": {
                "alpha": {"platform_id": "claude-code", "session_id": "alpha-session-1"},
                "beta": {"platform_id": "codex", "session_id": "beta-session-1"},
            },
            "candidate_delivery": {},
            "monitoring": {
                "enabled": False,
                "mode": "periodic",
                "status": "stopped",
                "stop_requested_at": (now - timedelta(seconds=2)).isoformat(timespec="milliseconds"),
                "stopped_at": now.isoformat(timespec="milliseconds"),
            },
        })
        candidate_word = self.workspace / "候选决策.docx"
        candidate_word.write_bytes(b"candidate-word")
        state["candidate_delivery"] = {
            "path": candidate_word.name,
            "opened": True,
            "opened_at": (now - timedelta(seconds=3)).isoformat(timespec="milliseconds"),
            "sha256": sha256_file(candidate_word),
        }

        instructions = {}
        for agent in state["expected_participants"]:
            instruction = _new_instruction(state, self.workspace, agent, "stop")
            payload = instruction.to_dict()
            payload["issued_at"] = (now - timedelta(seconds=30)).isoformat(timespec="milliseconds")
            unsigned = {key: value for key, value in payload.items() if key != "sha256"}
            payload["sha256"] = hashlib.sha256(json.dumps(
                unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            instruction = Instruction.from_dict(payload)
            write_instruction(self.workspace, instruction)
            instructions[agent] = instruction

        # Each stop marker is covered by a platform-signed attestation whose
        # instruction digest, participant session, and proof artifact all match.
        for agent, instruction in instructions.items():
            issued_at = datetime.fromisoformat(instruction.issued_at)
            stopped_at = max(datetime.now(timezone.utc).astimezone() - timedelta(seconds=2), issued_at + timedelta(seconds=1))
            proof_reference = ".multiagent/audit/platform-evidence/proofs/%s/%s.json" % (
                agent, instruction.instruction_id,
            )
            proof = {
                "discussion_id": instruction.discussion_id,
                "agent_id": agent,
                "instruction_id": instruction.instruction_id,
                "instruction_sha256": instruction.sha256,
                "platform_id": instruction.platform_id,
                "session_id": instruction.session_id,
                "stopped_at": stopped_at.isoformat(timespec="milliseconds"),
                "mechanism": "session_monitor_stop",
                "action_verified": True,
                "target": "current session monitor instance",
                "proof_reference": proof_reference,
            }
            proof_artifact = {key: proof[key] for key in (
                "discussion_id", "agent_id", "instruction_id", "instruction_sha256",
                "platform_id", "session_id", "stopped_at", "mechanism", "action_verified", "target",
            )}
            proof_path = self.workspace / proof_reference
            _write_json(proof_path, proof_artifact)
            proof["proof_sha256"] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
            _write_json(
                self.workspace / ".multiagent" / "audit" / "platform-evidence" / agent / (instruction.instruction_id + "-stop.json"),
                sign_payload(proof, private_key_path=self.private_key_path, key_id=self.key_id),
            )
            evidence = load_stop_attestation(self.workspace, instruction)
            marker_path = self.internal / "receipts" / agent / (instruction.instruction_id + "-stop-marker.txt")
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("stopped\n", encoding="utf-8")

            if agent in receipt_agents:
                completed_at = stopped_at + timedelta(seconds=1)
                receipt = {
                    "instruction_id": instruction.instruction_id,
                    "agent_id": agent,
                    "kind": "stop",
                    "status": "completed",
                    "runtime_version": instruction.runtime_version,
                    "state_revision": instruction.state_revision,
                    "at": completed_at.isoformat(timespec="milliseconds"),
                    "attempt": instruction.attempt,
                    "output_path": marker_path.relative_to(self.workspace).as_posix(),
                    "output_sha256": sha256_file(marker_path),
                    "isolation_evidence": evidence,
                }
                _write_json(receipt_path(self.workspace, agent, instruction.instruction_id, "completed"), receipt)

        state["revision"] = 5
        _write_json(self.state_path, state)
        return {agent: receipt_path(self.workspace, agent, instruction.instruction_id, "completed") for agent, instruction in instructions.items()}

    def _confirm_args(self) -> list[str]:
        return [
            "confirm_decision.py", "--state", str(self.state_path), "--actor", "alpha",
            "--platform-id", "claude-code", "--session-id", "alpha-session-1",
            "--candidate-id", "C-0001", "--confirm-text", "确认 C-0001",
        ]

    def _write_fake_docx(self, _md: str | Path, docx_path: str | Path) -> str:
        Path(docx_path).write_bytes(b"fake-docx")
        return str(docx_path)

    def _state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_candidate_markdown_and_manifest_are_internal_and_word_is_root_deliverable(self) -> None:
        with patch.object(export_docx, "convert_md", side_effect=self._write_fake_docx):
            record = export_docx.create_candidate_delivery(
                self.workspace,
                self.state,
                self.discussion,
                open_after=True,
                opener=lambda _path: True,
            )

        candidate_markdown = self.workspace / record["markdown_path"]
        candidate_word = self.workspace / record["path"]
        manifest = self.internal / "deliverables" / "candidate-manifest.json"
        self.assertTrue(record["opened"])
        self.assertTrue(record["opened_at"])
        self.assertEqual(record["path"], "候选决策.docx")
        self.assertTrue(candidate_markdown.is_relative_to(self.internal / "deliverables"))
        self.assertTrue(manifest.is_file())
        self.assertTrue(candidate_word.is_file())
        self.assertEqual(record["sha256"], sha256_file(candidate_word))
        self.assertEqual(record["markdown_sha256"], sha256_file(candidate_markdown))

    def test_no_open_candidate_is_never_marked_open(self) -> None:
        with patch.object(export_docx, "convert_md", side_effect=self._write_fake_docx), patch.object(
            export_docx, "open_document"
        ) as open_document:
            record = export_docx.create_candidate_delivery(
                self.workspace, self.state, self.discussion, open_after=False
            )

        self.assertFalse(record["opened"])
        self.assertIsNone(record["opened_at"])
        open_document.assert_not_called()

    def test_injected_opener_requires_explicit_true(self) -> None:
        target = self.workspace / "open-test.docx"
        self.assertTrue(export_docx.open_document(target, opener=lambda _path: True))
        self.assertFalse(export_docx.open_document(target, opener=lambda _path: False))
        self.assertFalse(export_docx.open_document(target, opener=lambda _path: None))

    def test_failed_formal_word_open_does_not_advance_to_delivered(self) -> None:
        state = dict(self.state)
        with patch.object(export_docx, "convert_md", side_effect=self._write_fake_docx), patch.object(
            export_docx, "open_document", return_value=False
        ):
            result, updated = export_docx.do_export(
                str(self.state_path), state, "alpha", str(self.workspace),
                str(self.discussion), None, False, open_after=True,
                trusted_execution=True,
            )

        self.assertNotEqual(result, export_docx.EXIT_OK)
        self.assertEqual(updated["stage"], "confirmed_decision")
        self.assertFalse(updated["formal_delivery"]["opened"])
        self.assertEqual(self._state()["stage"], "confirmed_decision")

    def test_formal_word_advances_only_after_successful_open(self) -> None:
        state = dict(self.state)
        with patch.object(export_docx, "convert_md", side_effect=self._write_fake_docx), patch.object(
            export_docx, "open_document", return_value=True
        ) as open_document:
            result, updated = export_docx.do_export(
                str(self.state_path), state, "alpha", str(self.workspace),
                str(self.discussion), None, False, open_after=True,
                trusted_execution=True,
            )

        self.assertEqual(result, export_docx.EXIT_OK)
        self.assertEqual(updated["stage"], "delivered")
        self.assertTrue(updated["formal_delivery"]["opened"])
        self.assertTrue((self.workspace / "最终决策.docx").is_file())
        open_document.assert_called_once()

    def test_confirm_decision_rejects_unopened_candidate_even_with_confirmation_text(self) -> None:
        state = dict(self.state)
        state.update({
            "stage": "user_confirmation",
            "candidate_delivery": {"path": "候选决策.docx", "opened": False},
            "monitoring": {"enabled": False, "mode": "periodic", "status": "stopped"},
        })
        _write_json(self.state_path, state)

        with patch.object(sys, "argv", [
            "confirm_decision.py", "--state", str(self.state_path), "--actor", "alpha",
            "--platform-id", "claude-code", "--session-id", "alpha-session-1",
            "--candidate-id", "C-0001", "--confirm-text", "确认 C-0001",
        ]):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_PRECOND)
        self.assertEqual(self._state()["stage"], "user_confirmation")
        self.assertEqual(self._state()["confirmed_decision_ids"], [])

    def test_confirm_decision_rejects_when_monitor_has_not_stopped(self) -> None:
        candidate_word = self.workspace / "候选决策.docx"
        candidate_word.write_bytes(b"candidate-word")
        state = dict(self.state)
        state.update({
            "stage": "user_confirmation",
            "candidate_delivery": {
                "path": "候选决策.docx", "opened": True,
                "opened_at": "2026-09-13T12:00:00+08:00", "sha256": sha256_file(candidate_word),
            },
            "monitoring": {"enabled": True, "mode": "periodic", "status": "active"},
        })
        _write_json(self.state_path, state)

        with patch.object(sys, "argv", [
            "confirm_decision.py", "--state", str(self.state_path), "--actor", "alpha",
            "--platform-id", "claude-code", "--session-id", "alpha-session-1",
            "--candidate-id", "C-0001", "--confirm-text", "确认 C-0001",
        ]):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_PRECOND)
        self.assertEqual(self._state()["stage"], "user_confirmation")

    def test_forged_stopped_state_without_receipts_is_rejected_byte_for_byte(self) -> None:
        self._prepare_confirmation_evidence(receipt_agents=())
        before = self.state_path.read_bytes()

        with patch.object(sys, "argv", self._confirm_args()):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_PRECOND)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self._state()["stage"], "user_confirmation")
        self.assertEqual(self._state()["confirmed_decision_ids"], [])

    def test_forged_stopped_state_with_one_missing_receipt_is_rejected_byte_for_byte(self) -> None:
        self._prepare_confirmation_evidence(receipt_agents=("alpha",))
        before = self.state_path.read_bytes()

        with patch.object(sys, "argv", self._confirm_args()):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_PRECOND)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self._state()["confirmed_decision_ids"], [])

    def test_forged_receipt_with_wrong_instruction_revision_is_rejected_byte_for_byte(self) -> None:
        receipt_paths = self._prepare_confirmation_evidence()
        receipt_path = receipt_paths["alpha"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["state_revision"] += 1
        _write_json(receipt_path, receipt)
        before = self.state_path.read_bytes()

        with patch.object(sys, "argv", self._confirm_args()):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_PRECOND)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self._state()["confirmed_decision_ids"], [])

    def test_duplicate_completed_stop_receipt_is_rejected_byte_for_byte(self) -> None:
        receipt_paths = self._prepare_confirmation_evidence()
        duplicate = json.loads(receipt_paths["alpha"].read_text(encoding="utf-8"))
        _write_json(self.internal / "receipts" / "alpha" / "duplicate-completed.json", duplicate)
        before = self.state_path.read_bytes()

        with patch.object(sys, "argv", self._confirm_args()):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_PRECOND)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self._state()["confirmed_decision_ids"], [])

    def test_confirmation_passes_with_every_valid_signed_stop_receipt(self) -> None:
        self._prepare_confirmation_evidence()

        with patch.object(sys, "argv", self._confirm_args()), patch.object(
            confirm_decision, "do_export", return_value=(confirm_decision.EXIT_OK, {})
        ):
            result = confirm_decision.main()

        self.assertEqual(result, confirm_decision.EXIT_OK)
        confirmed = self._state()
        self.assertEqual(confirmed["stage"], "confirmed_decision")
        self.assertEqual(confirmed["confirmed_decision_ids"], ["C-0001"])

    def test_confirmed_decision_retries_formal_delivery_after_open_failure(self) -> None:
        self._prepare_confirmation_evidence()
        state = self._state()
        state.update({"stage": "confirmed_decision", "confirmed_decision_ids": ["C-0001"]})
        _write_json(self.state_path, state)

        with patch.object(sys, "argv", [
            "confirm_decision.py", "--state", str(self.state_path), "--actor", "alpha",
            "--platform-id", "claude-code", "--session-id", "alpha-session-1",
            "--candidate-id", "C-0001", "--confirm-text", "确认 C-0001",
        ]), patch.object(export_docx, "convert_md", side_effect=self._write_fake_docx), patch.object(
            export_docx, "open_document", return_value=False
        ) as open_document:
            result = confirm_decision.main()

        self.assertEqual(result, export_docx.EXIT_ERR)
        self.assertEqual(self._state()["stage"], "confirmed_decision")
        open_document.assert_called_once()

    def test_confirm_wrong_session_fails_closed_without_changing_state_bytes(self) -> None:
        candidate_word = self.workspace / "候选决策.docx"
        candidate_word.write_bytes(b"candidate-word")
        state = dict(self.state)
        state.update({
            "stage": "user_confirmation",
            "candidate_delivery": {
                "path": "候选决策.docx", "opened": True,
                "opened_at": "2026-09-13T12:00:00+08:00", "sha256": sha256_file(candidate_word),
            },
            "monitoring": {
                "enabled": False, "mode": "periodic", "status": "stopped",
                "stop_requested_at": "2026-09-13T12:00:00+08:00",
            },
        })
        _write_json(self.state_path, state)
        before = self.state_path.read_bytes()

        with patch.object(sys, "argv", [
            "confirm_decision.py", "--state", str(self.state_path), "--actor", "alpha",
            "--platform-id", "claude-code", "--session-id", "wrong-session",
            "--candidate-id", "C-0001", "--confirm-text", "确认 C-0001",
        ]):
            result = confirm_decision.main()

        self.assertNotEqual(result, 0)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse(self._state()["confirmed_decision_ids"])

    def test_export_wrong_session_fails_closed_without_changing_state_bytes(self) -> None:
        state = dict(self.state)
        state["stage"] = "confirmed_decision"
        state["confirmed_decision_ids"] = ["C-0001"]
        _write_json(self.state_path, state)
        before = self.state_path.read_bytes()

        with patch.object(sys, "argv", [
            "export_docx.py", "--state", str(self.state_path), "--actor", "alpha",
            "--platform-id", "claude-code", "--session-id", "wrong-session", "--no-open",
        ]):
            result = export_docx.main()

        self.assertNotEqual(result, 0)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse((self.workspace / "最终决策.docx").exists())


if __name__ == "__main__":
    unittest.main()
