"""Compatibility alias for :mod:`app.entrypoint`.

The entrypoint verification use-case lives in :mod:`app.entrypoint`; this
module only re-exports its public surface under an alternate name.
"""

from __future__ import annotations

from .entrypoint import (  # noqa: F401
    Candidate,
    TargetOverlay,
    check_default_entrypoint,
    check_entrypoint,
    verify_default_command,
    verify_entrypoint,
)

__all__ = [
    "Candidate",
    "TargetOverlay",
    "verify_entrypoint",
    "check_entrypoint",
    "verify_default_command",
    "check_default_entrypoint",
]
