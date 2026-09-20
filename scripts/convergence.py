#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic convergence evaluation for multi-round cross responses.

This module deliberately has no filesystem, scheduler, or coordinator side
effects.  It consumes structured participant responses and returns a small
result object that a coordinator may later project into ``state.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


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
    "AgentResponse",
    "ConvergenceEvaluator",
    "ConvergenceResult",
    "FakeAgent",
    "LifecycleRun",
    "LifecycleStep",
    "build_three_fake_agents",
    "evaluate_convergence",
    "run_fake_agent_lifecycle",
    "simulate_three_agent_lifecycle",
]
