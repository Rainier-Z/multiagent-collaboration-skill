#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Schema gate and compatibility helpers for multi-round convergence.

Modern workflow note: semantic convergence is a constrained responsibility
of the coordinator.  The coordinator reads the complete round snapshot and
emits :class:`ConvergenceAssessment`; Python only validates that fixed JSON
schema and participant references.  It must not infer ``converged`` from
words such as ``"无"`` or from the number of quiet rounds.

``ConvergenceEvaluator`` remains below as a backwards-compatible test helper
for the original in-memory fixtures.  It is intentionally not the semantic
decision maker for the modern workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


NO_CONTENT_MARKERS = frozenset({"无", "暂无", "没有", "none", "n/a", "-"})


class ConvergenceSchemaError(ValueError):
    """Raised when a coordinator assessment does not satisfy the JSON schema."""


def _is_no_content(value: str) -> bool:
    return value.strip().casefold() in NO_CONTENT_MARKERS


def _normalize_content(value: Any) -> Any:
    """Normalize explicit no-content markers without making semantic decisions.

    The normalizer only changes representation.  In particular it never
    changes either of the semantic booleans ``converged`` and
    ``more_discussion``.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return "" if _is_no_content(stripped) else stripped
    if isinstance(value, list):
        normalized = [_normalize_content(item) for item in value]
        return [item for item in normalized if item not in ("", None, [])]
    if isinstance(value, tuple):
        return _normalize_content(list(value))
    if isinstance(value, dict):
        return {str(key): _normalize_content(item) for key, item in value.items()}
    return value


_ASSESSMENT_FIELDS = (
    "round",
    "based_on_snapshot_path",
    "based_on_snapshot_sha256",
    "based_on_revision",
    "new_substantive_issues",
    "unanswered_arguments",
    "new_evidence",
    "remaining_disagreements",
    "positions",
    "value_conflicts",
    "more_discussion",
    "requires_human_decision",
    "converged",
    "reason",
)


@dataclass(frozen=True)
class ConvergenceAssessment:
    """Coordinator-authored semantic assessment for one response round.

    This is the modern ``.multiagent/convergence/round-N.json`` contract.
    ``positions`` maps a legal participant id to that participant's stated
    position(s), so the schema gate can catch references to unknown agents.
    No field is derived from another field; the two booleans are explicit
    coordinator judgments.
    """

    round: int
    based_on_snapshot_path: str
    based_on_snapshot_sha256: str
    based_on_revision: int
    new_substantive_issues: tuple[str, ...] = ()
    unanswered_arguments: tuple[str, ...] = ()
    new_evidence: tuple[str, ...] = ()
    remaining_disagreements: tuple[str, ...] = ()
    positions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    value_conflicts: tuple[str, ...] = ()
    more_discussion: bool = False
    requires_human_decision: bool = False
    converged: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.round, bool) or not isinstance(self.round, int) or self.round < 1:
            raise ConvergenceSchemaError("round must be a positive integer")
        if not isinstance(self.based_on_snapshot_path, str) or not self.based_on_snapshot_path.strip():
            raise ConvergenceSchemaError("based_on_snapshot_path must be a non-empty string")
        if not isinstance(self.based_on_snapshot_sha256, str) or len(self.based_on_snapshot_sha256) != 64:
            raise ConvergenceSchemaError("based_on_snapshot_sha256 must be a SHA-256 string")
        if isinstance(self.based_on_revision, bool) or not isinstance(self.based_on_revision, int) or self.based_on_revision < 1:
            raise ConvergenceSchemaError("based_on_revision must be a positive integer")
        for name in (
            "new_substantive_issues",
            "unanswered_arguments",
            "new_evidence",
            "remaining_disagreements",
            "value_conflicts",
        ):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise ConvergenceSchemaError(f"{name} must be an array of strings")
            normalized = _normalize_content(list(value))
            if any(not isinstance(item, str) for item in normalized):
                raise ConvergenceSchemaError(f"{name} must be an array of strings")
            object.__setattr__(self, name, tuple(normalized))

        if not isinstance(self.positions, Mapping):
            raise ConvergenceSchemaError("positions must be an object keyed by participant id")
        normalized_positions: dict[str, tuple[str, ...]] = {}
        for participant, values in self.positions.items():
            if not isinstance(participant, str) or not participant.strip():
                raise ConvergenceSchemaError("positions keys must be non-empty participant ids")
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple)):
                raise ConvergenceSchemaError("each positions value must be an array of strings")
            normalized_values = _normalize_content(list(values))
            if any(not isinstance(item, str) for item in normalized_values):
                raise ConvergenceSchemaError("each positions value must be an array of strings")
            normalized_positions[participant.strip()] = tuple(normalized_values)
        object.__setattr__(self, "positions", normalized_positions)

        for name in ("more_discussion", "requires_human_decision", "converged"):
            if not isinstance(getattr(self, name), bool):
                raise ConvergenceSchemaError(f"{name} must be boolean")
        if not isinstance(self.reason, str):
            raise ConvergenceSchemaError("reason must be a string")
        object.__setattr__(self, "reason", _normalize_content(self.reason))

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        participant_ids: Sequence[str] | None = None,
    ) -> "ConvergenceAssessment":
        """Validate and normalize a coordinator JSON object.

        ``participant_ids`` is the authoritative legal-reference set.  When
        omitted, the gate still validates shape but cannot reject an unknown
        participant.  The optional long-form key from early drafts is accepted
        as an input alias and always serialized as ``more_discussion``.
        """
        if not isinstance(payload, Mapping):
            raise ConvergenceSchemaError("assessment must be a JSON object")
        missing = [name for name in _ASSESSMENT_FIELDS if name not in payload and not (
            name == "more_discussion" and "would_more_discussion_add_information" in payload
        )]
        if missing:
            raise ConvergenceSchemaError("missing required fields: " + ", ".join(missing))
        unknown = sorted(set(payload) - set(_ASSESSMENT_FIELDS) - {"would_more_discussion_add_information"})
        if unknown:
            raise ConvergenceSchemaError("unknown fields: " + ", ".join(map(str, unknown)))
        more_discussion = payload.get("more_discussion", payload.get("would_more_discussion_add_information"))
        assessment = cls(
            round=payload.get("round"),  # type: ignore[arg-type]
            based_on_snapshot_path=payload.get("based_on_snapshot_path"),  # type: ignore[arg-type]
            based_on_snapshot_sha256=payload.get("based_on_snapshot_sha256"),  # type: ignore[arg-type]
            based_on_revision=payload.get("based_on_revision"),  # type: ignore[arg-type]
            new_substantive_issues=payload.get("new_substantive_issues"),  # type: ignore[arg-type]
            unanswered_arguments=payload.get("unanswered_arguments"),  # type: ignore[arg-type]
            new_evidence=payload.get("new_evidence"),  # type: ignore[arg-type]
            remaining_disagreements=payload.get("remaining_disagreements"),  # type: ignore[arg-type]
            positions=payload.get("positions"),  # type: ignore[arg-type]
            value_conflicts=payload.get("value_conflicts"),  # type: ignore[arg-type]
            more_discussion=more_discussion,  # type: ignore[arg-type]
            requires_human_decision=payload.get("requires_human_decision"),  # type: ignore[arg-type]
            converged=payload.get("converged"),  # type: ignore[arg-type]
            reason=payload.get("reason"),  # type: ignore[arg-type]
        )
        legal = None if participant_ids is None else {
            item.strip() for item in participant_ids if isinstance(item, str) and item.strip()
        }
        if participant_ids is not None and len(legal or ()) != len(tuple(participant_ids)):
            raise ConvergenceSchemaError("participant_ids must contain non-empty strings")
        if legal is not None:
            unknown_participants = sorted(set(assessment.positions) - legal)
            if unknown_participants:
                raise ConvergenceSchemaError(
                    "positions reference unknown participants: " + ", ".join(unknown_participants)
                )
        return assessment

    def to_dict(self) -> dict[str, object]:
        """Return the canonical fixed-schema JSON-compatible mapping."""
        return {
            "round": self.round,
            "based_on_snapshot_path": self.based_on_snapshot_path,
            "based_on_snapshot_sha256": self.based_on_snapshot_sha256,
            "based_on_revision": self.based_on_revision,
            "new_substantive_issues": list(self.new_substantive_issues),
            "unanswered_arguments": list(self.unanswered_arguments),
            "new_evidence": list(self.new_evidence),
            "remaining_disagreements": list(self.remaining_disagreements),
            "positions": {key: list(values) for key, values in self.positions.items()},
            "value_conflicts": list(self.value_conflicts),
            "more_discussion": self.more_discussion,
            "requires_human_decision": self.requires_human_decision,
            "converged": self.converged,
            "reason": self.reason,
        }


def validate_convergence_assessment(
    payload: Mapping[str, object],
    *,
    participant_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    """Schema-gate a coordinator assessment and return canonical JSON data."""
    return ConvergenceAssessment.from_mapping(payload, participant_ids=participant_ids).to_dict()


# Short aliases make the gate discoverable to callers without changing the
# canonical API used by the modern orchestrator integration.
validate_convergence = validate_convergence_assessment


def _items(values: Iterable[str] | str | None) -> tuple[str, ...]:
    """Normalize labels while preserving their first-seen order."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("response labels must be strings")
        value = value.strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


@dataclass(frozen=True)
class AgentResponse:
    """One participant's structured response for one cross-response round."""

    agent_id: str
    round_number: int
    consensus: tuple[str, ...] = ()
    unresolved_disagreements: tuple[str, ...] = ()
    new_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        agent_id = self.agent_id.strip() if isinstance(self.agent_id, str) else ""
        if not agent_id:
            raise ValueError("agent_id must be a non-empty string")
        if isinstance(self.round_number, bool) or self.round_number < 1:
            raise ValueError("round_number must be a positive integer")
        object.__setattr__(self, "agent_id", agent_id)
        object.__setattr__(self, "consensus", _items(self.consensus))
        object.__setattr__(self, "unresolved_disagreements", _items(self.unresolved_disagreements))
        object.__setattr__(self, "new_issues", _items(self.new_issues))

    @property
    def round(self) -> int:
        """Short alias used by serialized fixtures and callers."""
        return self.round_number

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "AgentResponse":
        """Build a response from a JSON-compatible fixture mapping."""
        if not isinstance(value, Mapping):
            raise TypeError("response must be a mapping")
        round_number = value.get("round_number", value.get("round"))
        if round_number is None:
            raise ValueError("response is missing round_number")
        return cls(
            agent_id=value.get("agent_id", ""),  # type: ignore[arg-type]
            round_number=round_number,  # type: ignore[arg-type]
            consensus=value.get("consensus", ()),  # type: ignore[arg-type]
            unresolved_disagreements=value.get("unresolved_disagreements", value.get("disagreements", ())),  # type: ignore[arg-type]
            new_issues=value.get("new_issues", ()),  # type: ignore[arg-type]
        )


@dataclass
class ConvergenceResult:
    """Structured output of one convergence evaluation.

    ``converged`` means the machine-level cross-response criteria are met.
    It never means that a decision was silently accepted: callers must still
    honor ``requires_human_decision`` and obtain Rainier's explicit decision.
    """

    round_number: int
    new_issues: list[str] = field(default_factory=list)
    unresolved_disagreements: list[str] = field(default_factory=list)
    consensus: list[str] = field(default_factory=list)
    requires_human_decision: bool = False
    converged: bool = False

    @property
    def round(self) -> int:
        return self.round_number

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "new_issues": list(self.new_issues),
            "unresolved_disagreements": list(self.unresolved_disagreements),
            "consensus": list(self.consensus),
            "requires_human_decision": self.requires_human_decision,
            "converged": self.converged,
        }


class ConvergenceEvaluator:
    """Evaluate a sequence of complete cross-response rounds.

    A round is converged only when all expected participants respond, the
    minimum round count is met, no disagreement is still reported, and the
    configured number of quiet (no-new-issue) rounds has elapsed.  A human
    decision is requested once those machine criteria converge, or when the
    maximum round count is exhausted while a disagreement remains.
    """

    def __init__(
        self,
        *,
        min_response_rounds: int = 1,
        max_response_rounds: int = 3,
        no_new_issue_rounds: int = 1,
        participant_ids: Sequence[str] | None = None,
    ) -> None:
        if min_response_rounds < 1:
            raise ValueError("min_response_rounds must be at least 1")
        if max_response_rounds < min_response_rounds:
            raise ValueError("max_response_rounds must be >= min_response_rounds")
        if no_new_issue_rounds < 1:
            raise ValueError("no_new_issue_rounds must be at least 1")
        participants = tuple(dict.fromkeys(participant_ids or ()))
        if participants and any(not isinstance(item, str) or not item.strip() for item in participants):
            raise ValueError("participant_ids must contain non-empty strings")
        self.min_response_rounds = min_response_rounds
        self.max_response_rounds = max_response_rounds
        self.no_new_issue_rounds = no_new_issue_rounds
        self.participant_ids = participants
        self._rounds: dict[int, tuple[AgentResponse, ...]] = {}
        self._seen_issues: set[str] = set()
        self._quiet_rounds = 0
        self.last_result: ConvergenceResult | None = None

    @property
    def rounds(self) -> Mapping[int, tuple[AgentResponse, ...]]:
        """Read-only view of accepted rounds for audit/test inspection."""
        return dict(self._rounds)

    def reset(self) -> None:
        self._rounds.clear()
        self._seen_issues.clear()
        self._quiet_rounds = 0
        self.last_result = None

    def evaluate(self, responses: Iterable[AgentResponse | Mapping[str, object]]) -> ConvergenceResult:
        """Evaluate all supplied rounds in order and return the latest result."""
        normalized = [self._coerce(item) for item in responses]
        if not normalized:
            raise ValueError("at least one response is required")
        by_round: dict[int, list[AgentResponse]] = {}
        for response in normalized:
            by_round.setdefault(response.round_number, []).append(response)
        result: ConvergenceResult | None = None
        for round_number in sorted(by_round):
            result = self.evaluate_round(by_round[round_number])
        assert result is not None
        return result

    def evaluate_round(
        self,
        responses: Iterable[AgentResponse | Mapping[str, object]],
    ) -> ConvergenceResult:
        """Evaluate one complete round; duplicate rounds are rejected."""
        current = tuple(self._coerce(item) for item in responses)
        if not current:
            raise ValueError("a response round cannot be empty")
        round_numbers = {item.round_number for item in current}
        if len(round_numbers) != 1:
            raise ValueError("all responses in a round must share round_number")
        round_number = current[0].round_number
        if round_number in self._rounds:
            raise ValueError("round %d has already been evaluated" % round_number)

        ids = [item.agent_id for item in current]
        if len(set(ids)) != len(ids):
            raise ValueError("each participant may respond only once per round")
        if not self.participant_ids:
            self.participant_ids = tuple(ids)
        if set(ids) != set(self.participant_ids):
            missing = sorted(set(self.participant_ids) - set(ids))
            unexpected = sorted(set(ids) - set(self.participant_ids))
            raise ValueError("incomplete participant round (missing=%s, unexpected=%s)" % (missing, unexpected))
        if round_number > self.max_response_rounds:
            raise ValueError("round %d exceeds max_response_rounds=%d" % (round_number, self.max_response_rounds))

        current_issues: list[str] = []
        for response in current:
            for issue in response.new_issues:
                if issue not in current_issues:
                    current_issues.append(issue)
        new_issues = [issue for issue in current_issues if issue not in self._seen_issues]
        self._seen_issues.update(current_issues)
        if new_issues:
            self._quiet_rounds = 0
        else:
            self._quiet_rounds += 1

        unresolved: list[str] = []
        for response in current:
            for disagreement in response.unresolved_disagreements:
                if disagreement not in unresolved:
                    unresolved.append(disagreement)

        # Consensus is an assertion shared by every participant in this round.
        common = set(current[0].consensus)
        for response in current[1:]:
            common.intersection_update(response.consensus)
        consensus = [item for item in current[0].consensus if item in common]

        converged = (
            round_number >= self.min_response_rounds
            and self._quiet_rounds >= self.no_new_issue_rounds
            and not unresolved
        )
        requires_human = converged or (
            round_number >= self.max_response_rounds and bool(unresolved)
        )
        result = ConvergenceResult(
            round_number=round_number,
            new_issues=new_issues,
            unresolved_disagreements=unresolved,
            consensus=consensus,
            requires_human_decision=requires_human,
            converged=converged,
        )
        self._rounds[round_number] = current
        self.last_result = result
        return result

    @staticmethod
    def _coerce(value: AgentResponse | Mapping[str, object]) -> AgentResponse:
        if isinstance(value, AgentResponse):
            return value
        return AgentResponse.from_mapping(value)


@dataclass(frozen=True)
class FakeAgent:
    """Deterministic participant used by the lifecycle fixture."""

    agent_id: str
    responses: Mapping[int, AgentResponse]

    def respond(self, round_number: int) -> AgentResponse:
        try:
            response = self.responses[round_number]
        except KeyError as exc:
            raise ValueError("%s has no response for round %d" % (self.agent_id, round_number)) from exc
        if response.agent_id != self.agent_id:
            raise ValueError("fixture response identity does not match fake agent")
        return response


@dataclass(frozen=True)
class LifecycleStep:
    event: str
    round_number: int | None = None
    result: ConvergenceResult | None = None
    detail: str | None = None


@dataclass
class LifecycleRun:
    steps: list[LifecycleStep]
    result: ConvergenceResult
    final_decision: str | None
    stopped: bool

    @property
    def event_names(self) -> list[str]:
        return [step.event for step in self.steps]


def build_three_fake_agents() -> tuple[FakeAgent, FakeAgent, FakeAgent]:
    """Return the stable three-agent fixture used by ``evals/test_convergence``."""
    ids = ("agent-a", "agent-b", "agent-c")
    agents: list[FakeAgent] = []
    for agent_id in ids:
        agents.append(FakeAgent(agent_id, {
            1: AgentResponse(
                agent_id,
                1,
                consensus=("bounded rollout",),
                unresolved_disagreements=("latency-vs-safety",),
                new_issues=("retention audit",),
            ),
            2: AgentResponse(
                agent_id,
                2,
                consensus=("bounded rollout", "audit before launch"),
            ),
        }))
    return tuple(agents)  # type: ignore[return-value]


def run_fake_agent_lifecycle(
    agents: Sequence[FakeAgent] | None = None,
    *,
    evaluator: ConvergenceEvaluator | None = None,
    final_decision: str = "approve consensus",
) -> LifecycleRun:
    """Run round(s) through convergence, review, decision, and stop.

    This is an in-memory exercise only.  The ``stop`` event is a lifecycle
    marker; it does not claim to stop a real platform monitor or Automation.
    """
    participants = tuple(agents or build_three_fake_agents())
    if not participants:
        raise ValueError("at least one fake agent is required")
    if evaluator is None:
        evaluator = ConvergenceEvaluator(
            min_response_rounds=2,
            max_response_rounds=3,
            no_new_issue_rounds=1,
            participant_ids=tuple(agent.agent_id for agent in participants),
        )
    steps: list[LifecycleStep] = []
    result: ConvergenceResult | None = None
    for round_number in range(1, evaluator.max_response_rounds + 1):
        round_responses = [agent.respond(round_number) for agent in participants]
        result = evaluator.evaluate_round(round_responses)
        steps.append(LifecycleStep("cross_response_round", round_number, result))
        if result.converged:
            steps.extend((
                LifecycleStep("converged", round_number, result),
                LifecycleStep("human_review", round_number, result),
            ))
            decision = final_decision.strip()
            if not decision:
                raise ValueError("final_decision must be non-empty")
            steps.append(LifecycleStep("final_decision", round_number, result, decision))
            steps.append(LifecycleStep("stop", round_number, result))
            return LifecycleRun(steps, result, decision, True)
    assert result is not None
    return LifecycleRun(steps, result, None, False)


def evaluate_convergence(
    responses: Iterable[AgentResponse | Mapping[str, object]],
    *,
    min_response_rounds: int = 1,
    max_response_rounds: int = 3,
    no_new_issue_rounds: int = 1,
    participant_ids: Sequence[str] | None = None,
) -> ConvergenceResult:
    """Stateless convenience wrapper for evaluating JSON-compatible rounds."""
    evaluator = ConvergenceEvaluator(
        min_response_rounds=min_response_rounds,
        max_response_rounds=max_response_rounds,
        no_new_issue_rounds=no_new_issue_rounds,
        participant_ids=participant_ids,
    )
    return evaluator.evaluate(responses)


simulate_three_agent_lifecycle = run_fake_agent_lifecycle


__all__ = [
    "ConvergenceAssessment",
    "ConvergenceSchemaError",
    "NO_CONTENT_MARKERS",
    "AgentResponse",
    "ConvergenceEvaluator",
    "ConvergenceResult",
    "FakeAgent",
    "LifecycleRun",
    "LifecycleStep",
    "build_three_fake_agents",
    "evaluate_convergence",
    "validate_convergence",
    "validate_convergence_assessment",
    "run_fake_agent_lifecycle",
    "simulate_three_agent_lifecycle",
]
