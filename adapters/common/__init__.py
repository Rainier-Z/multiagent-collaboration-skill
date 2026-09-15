"""Shared, platform-neutral wake contract."""

from .wake_protocol import WakeAdapter, WakeRequest, WakeResult

__all__ = ["WakeAdapter", "WakeRequest", "WakeResult"]
