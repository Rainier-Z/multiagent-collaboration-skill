"""Codex wake adapter with an explicit three-state activation bridge.

Injected dispatchers receive the requested platform/session IDs unchanged;
``accepted`` remains a dispatch acknowledgment, not proof of execution.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.common.wake_protocol import (
    ActivationResult,
    WakeRequest,
    WakeResult,
    activation_failed,
    manual_activation_required,
    unavailable,
)


class CodexWakeAdapter:
    platform = "Codex"

    def __init__(self, dispatcher: Callable[[WakeRequest], ActivationResult | WakeResult] | None = None) -> None:
        self._dispatcher = dispatcher

    def activate(self, request: WakeRequest, *, idempotency_key: str | None = None) -> ActivationResult:
        if self._dispatcher is None:
            return manual_activation_required(self.platform)
        try:
            result = self._dispatcher(request)
        except Exception as exc:
            return activation_failed(self.platform, f"dispatcher raised {type(exc).__name__}")
        if isinstance(result, ActivationResult):
            return result
        if isinstance(result, WakeResult):
            if result.status == "accepted":
                return ActivationResult("activated", result.detail, result.evidence)
            if result.status == "unavailable":
                return manual_activation_required(self.platform, result.detail)
            return activation_failed(self.platform, result.detail)
        return activation_failed(self.platform, "dispatcher returned an invalid result")

    def wake(self, request: WakeRequest) -> WakeResult:
        result = self.activate(request)
        if result.status == "activated":
            return WakeResult("accepted", result.detail, result.evidence)
        if result.status == "manual_activation_required":
            return unavailable(self.platform, result.detail)
        return WakeResult("rejected", result.detail, result.evidence)
