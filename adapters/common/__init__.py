"""Shared, platform-neutral wake contract."""

from .wake_protocol import (
    ActivationBridge,
    ActivationResult,
    WakeAdapter,
    WakeRequest,
    WakeResult,
)

__all__ = ["ActivationBridge", "ActivationResult", "WakeAdapter", "WakeRequest", "WakeResult"]
