"""OpenClaw wake and trusted stop adapter contracts."""

from .wake_adapter import OpenClawWakeAdapter
from .stop_adapter import OpenClawStopError, stop_openclaw_automation

__all__ = ["OpenClawWakeAdapter", "OpenClawStopError", "stop_openclaw_automation"]
