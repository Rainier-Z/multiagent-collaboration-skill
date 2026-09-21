#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三 Agent 多轮 cross_response 收敛评估器的可复现实习。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from convergence import (  # type: ignore[import-not-found]
    AgentResponse,
    ConvergenceAssessment,
    ConvergenceEvaluator,
    ConvergenceSchemaError,
    build_three_fake_agents,
    evaluate_convergence,
    run_fake_agent_lifecycle,
    validate_convergence_assessment,
)


def modern_assessment(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "round": 2,
        "new_substantive_issues": ["暂无"],
        "unanswered_arguments": ["没有"],
        "new_evidence": ["N/A"],
        "remaining_disagreements": ["-"],
        "positions": {"agent-a": ["bounded rollout"], "agent-b": ["bounded rollout"]},
        "value_conflicts": ["无"],
        "more_discussion": False,
        "requires_human_decision": True,
        "converged": True,
        "reason": "暂无",
    }
    payload.update(overrides)
    return payload


class ModernConvergenceSchemaTests(unittest.TestCase):
    def test_gate_normalizes_no_content_but_preserves_explicit_semantic_booleans(self) -> None:
        assessment = ConvergenceAssessment.from_mapping(
            modern_assessment(),
            participant_ids=("agent-a", "agent-b", "agent-c"),
        )

        self.assertEqual(assessment.new_substantive_issues, ())
        self.assertEqual(assessment.unanswered_arguments, ())
        self.assertEqual(assessment.new_evidence, ())
        self.assertEqual(assessment.remaining_disagreements, ())
        self.assertEqual(assessment.value_conflicts, ())
        self.assertEqual(assessment.reason, "")
        self.assertTrue(assessment.converged)
        self.assertTrue(assessment.requires_human_decision)
        self.assertFalse(assessment.more_discussion)

    def test_gate_rejects_unknown_participant_reference(self) -> None:
        with self.assertRaises(ConvergenceSchemaError):
            validate_convergence_assessment(
                modern_assessment(positions={"agent-x": ["unknown"]}),
                participant_ids=("agent-a", "agent-b", "agent-c"),
            )

    def test_gate_rejects_missing_fields_and_wrong_boolean_types(self) -> None:
        missing = modern_assessment()
        del missing["reason"]
        with self.assertRaises(ConvergenceSchemaError):
            validate_convergence_assessment(missing)
        with self.assertRaises(ConvergenceSchemaError):
            validate_convergence_assessment(modern_assessment(converged="yes"))

    def test_gate_does_not_infer_convergence_from_empty_content(self) -> None:
        result = validate_convergence_assessment(
            modern_assessment(
                converged=False,
                requires_human_decision=False,
                more_discussion=True,
                reason="无新信息，但协调者要求继续",
            )
        )
        self.assertFalse(result["converged"])
        self.assertFalse(result["requires_human_decision"])
        self.assertTrue(result["more_discussion"])

    def test_gate_accepts_early_long_form_more_discussion_alias(self) -> None:
        payload = modern_assessment()
        del payload["more_discussion"]
        payload["would_more_discussion_add_information"] = False
        result = validate_convergence_assessment(payload)
        self.assertFalse(result["more_discussion"])


class ConvergenceEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = ConvergenceEvaluator(
            min_response_rounds=2,
            max_response_rounds=3,
            no_new_issue_rounds=1,
            participant_ids=("agent-a", "agent-b", "agent-c"),
        )

    def test_round_one_exposes_new_issue_and_disagreement(self) -> None:
        result = self.evaluator.evaluate_round(
            [
                AgentResponse("agent-a", 1, ("bounded rollout",), ("latency-vs-safety",), ("retention audit",)),
                AgentResponse("agent-b", 1, ("bounded rollout",), ("latency-vs-safety",), ("retention audit",)),
                AgentResponse("agent-c", 1, ("bounded rollout",), ("latency-vs-safety",), ("retention audit",)),
            ]
        )

        self.assertEqual(result.round_number, 1)
        self.assertEqual(result.new_issues, ["retention audit"])
        self.assertEqual(result.unresolved_disagreements, ["latency-vs-safety"])
        self.assertEqual(result.consensus, ["bounded rollout"])
        self.assertFalse(result.converged)
        self.assertFalse(result.requires_human_decision)

    def test_round_two_without_new_issues_converges_and_requests_human_review(self) -> None:
        self.evaluator.evaluate_round(
            [
                AgentResponse("agent-a", 1, ("bounded rollout",), ("latency-vs-safety",), ("retention audit",)),
                AgentResponse("agent-b", 1, ("bounded rollout",), ("latency-vs-safety",), ("retention audit",)),
                AgentResponse("agent-c", 1, ("bounded rollout",), ("latency-vs-safety",), ("retention audit",)),
            ]
        )
        result = self.evaluator.evaluate_round(
            [
                AgentResponse("agent-a", 2, ("bounded rollout", "audit before launch")),
                AgentResponse("agent-b", 2, ("bounded rollout", "audit before launch")),
                AgentResponse("agent-c", 2, ("bounded rollout", "audit before launch")),
            ]
        )

        self.assertEqual(result.new_issues, [])
        self.assertEqual(result.unresolved_disagreements, [])
        self.assertEqual(result.consensus, ["bounded rollout", "audit before launch"])
        self.assertTrue(result.converged)
        self.assertTrue(result.requires_human_decision)
        self.assertEqual(
            result.to_dict(),
            {
                "round_number": 2,
                "new_issues": [],
                "unresolved_disagreements": [],
                "consensus": ["bounded rollout", "audit before launch"],
                "requires_human_decision": True,
                "converged": True,
            },
        )

    def test_result_is_json_serializable(self) -> None:
        result = self.evaluator.evaluate_round(
            [AgentResponse(agent_id, 1, ("bounded rollout",)) for agent_id in ("agent-a", "agent-b", "agent-c")]
        )
        self.assertIsInstance(json.dumps(result.to_dict()), str)

    def test_json_fixture_mappings_are_supported_by_stateless_helper(self) -> None:
        result = evaluate_convergence(
            [
                {"agent_id": "agent-a", "round": 1, "consensus": ["bounded rollout"]},
                {"agent_id": "agent-b", "round": 1, "consensus": ["bounded rollout"]},
                {"agent_id": "agent-c", "round": 1, "consensus": ["bounded rollout"]},
            ],
            min_response_rounds=1,
            participant_ids=("agent-a", "agent-b", "agent-c"),
        )
        self.assertTrue(result.converged)
        self.assertEqual(result.consensus, ["bounded rollout"])


class FakeAgentLifecycleTests(unittest.TestCase):
    def test_three_agents_follow_round_one_round_two_human_review_decision_and_stop(self) -> None:
        agents = build_three_fake_agents()
        run = run_fake_agent_lifecycle(agents, final_decision="approve bounded rollout")

        self.assertEqual(
            [step.event for step in run.steps],
            ["cross_response_round", "cross_response_round", "converged", "human_review", "final_decision", "stop"],
        )
        self.assertEqual([step.round_number for step in run.steps[:2]], [1, 2])
        self.assertTrue(run.result.converged)
        self.assertTrue(run.result.requires_human_decision)
        self.assertEqual(run.final_decision, "approve bounded rollout")
        self.assertTrue(run.stopped)
        self.assertLess(
            run.event_names.index("final_decision"),
            run.event_names.index("stop"),
        )


if __name__ == "__main__":
    unittest.main()
