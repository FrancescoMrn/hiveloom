"""Error types and process exit codes shared across hiveloom.

Exit codes are part of the CLI contract (see section 6 of the build spec):
agents driving the CLI branch on them, so they are defined once here.
"""

from __future__ import annotations


class ExitCode:
    """Canonical process exit codes for the ``hiveloom`` CLI."""

    OK = 0
    VERIFY_FAILED = 1
    GUARDRAIL_HALT = 2
    SPEC_ERROR = 3
    RUNTIME_ERROR = 4


class HiveloomError(Exception):
    """Base class for all hiveloom errors."""


class SpecError(HiveloomError):
    """Raised when a harness spec is invalid or cannot be loaded.

    The message is expected to be human-readable and actionable — it is
    surfaced verbatim to the CLI user (or the agent driving the CLI).
    """


class CatalogError(HiveloomError):
    """Raised when a referenced builtin (tool/guardrail/validator/policy) is unknown."""
