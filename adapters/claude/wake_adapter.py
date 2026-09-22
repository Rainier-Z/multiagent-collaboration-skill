"""Claude Code wake adapter.

No Claude Code wake API is bundled or assumed.  The default therefore reports
``unavailable``.  A host may inject a verified dispatcher after recording
platform-specific end-to-end evidence; the request's target platform/session
IDs are forwarded unchanged.  An ``accepted`` result means only that the host
accepted a minimal locator request, never that the participant ran.
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


class ClaudeCodeWakeAdapter:
    platform = "Claude Code"

    def __init__(self, dispatcher: Callable[[WakeRequest], ActivationResult | WakeResult] | None = None) -> None:
        self._dispatcher = dispatcher

    def activate(self, request: WakeRequest, *, idempotency_key: str | None = None) -> ActivationResult:
        """Attempt explicit activation; a scanner is never a dispatcher."""
        if self._dispatcher is None:
            return manual_activation_required(self.platform)
        try:
            result = self._dispatcher(request)
        except Exception as exc:
            return activation_failed(self.platform, f"dispatcher raised {type(exc).__name__}")
        if isinstance(result, ActivationResult):
            return result
        if isinstance(result, WakeResult):
            mapped = {
                "accepted": ActivationResult("activated", result.detail, result.evidence),
                "unavailable": manual_activation_required(self.platform, result.detail),
                "rejected": activation_failed(self.platform, result.detail),
            }
            return mapped[result.status]
        return activation_failed(self.platform, "dispatcher returned an invalid result")

    def wake(self, request: WakeRequest) -> WakeResult:
        result = self.activate(request)
        if result.status == "activated":
            return WakeResult("accepted", result.detail, result.evidence)
        if result.status == "manual_activation_required":
            return unavailable(self.platform, result.detail)
        return WakeResult("rejected", result.detail, result.evidence)
