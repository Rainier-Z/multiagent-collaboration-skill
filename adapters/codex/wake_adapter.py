"""Codex wake adapter with an honest unavailable default.

Injected dispatchers receive the requested platform/session IDs unchanged;
``accepted`` remains a dispatch acknowledgment, not proof of execution.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.common.wake_protocol import WakeRequest, WakeResult, unavailable


class CodexWakeAdapter:
    platform = "Codex"

    def __init__(self, dispatcher: Callable[[WakeRequest], WakeResult] | None = None) -> None:
        self._dispatcher = dispatcher

    def wake(self, request: WakeRequest) -> WakeResult:
        if self._dispatcher is None:
            return unavailable(self.platform)
        result = self._dispatcher(request)
        if not isinstance(result, WakeResult):
            return WakeResult("rejected", "verified dispatcher returned an invalid result")
        return result
