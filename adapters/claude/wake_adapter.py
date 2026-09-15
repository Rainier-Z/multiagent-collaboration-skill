"""Claude Code wake adapter.

No Claude Code wake API is bundled or assumed.  The default therefore reports
``unavailable``.  A host may inject a verified dispatcher after recording
platform-specific end-to-end evidence; the request's target platform/session
IDs are forwarded unchanged.  An ``accepted`` result means only that the host
accepted a minimal locator request, never that the participant ran.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.common.wake_protocol import WakeRequest, WakeResult, unavailable


class ClaudeCodeWakeAdapter:
    platform = "Claude Code"

    def __init__(self, dispatcher: Callable[[WakeRequest], WakeResult] | None = None) -> None:
        self._dispatcher = dispatcher

    def wake(self, request: WakeRequest) -> WakeResult:
        if self._dispatcher is None:
            return unavailable(self.platform)
        result = self._dispatcher(request)
        if not isinstance(result, WakeResult):
            return WakeResult("rejected", "verified dispatcher returned an invalid result")
        return result
