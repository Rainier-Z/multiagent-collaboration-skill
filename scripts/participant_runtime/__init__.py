"""Project-scoped Participant Runtime package.

This package is copied into each discussion workspace by the coordinator.  It
is deliberately small: it validates and consumes instructions but does not
poll, advance workflow state, or call a model.
"""

from .protocol import Instruction, Receipt, RuntimeManifest

__all__ = ["Instruction", "Receipt", "RuntimeManifest"]
