"""Compatibility alias for :mod:`app.entrypoint`."""

from __future__ import annotations

from .entrypoint import (  # noqa: F401
    Candidate,
    TargetOverlay,
    build_candidates,
    check_default_entrypoint,
    check_entrypoint,
    permission_class,
    verify_default_command,
    verify_entrypoint,
    working_dir_key,
)

__all__ = [
    "Candidate",
    "TargetOverlay",
    "build_candidates",
    "permission_class",
    "working_dir_key",
    "verify_entrypoint",
    "check_entrypoint",
    "verify_default_command",
    "check_default_entrypoint",
]
