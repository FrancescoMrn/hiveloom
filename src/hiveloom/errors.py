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


class ProposalQueueError(SpecError):
    """Raised for a caller mistake against the proposal queue.

    Covers an unknown proposal id, a proposal that is no longer pending, or a
    proposal whose stored ``spec_version_hash`` no longer matches the live
    harness (the harness changed since the proposal was drafted — regenerate
    it). Subclasses :class:`SpecError` so the CLI's ``_guard`` maps it to
    :class:`ExitCode.SPEC_ERROR`, same as other actionable caller errors.

    Distinct from :class:`hiveloom.evolve.evolver.ProposalError`, which is
    raised when the *model's* proposal content is malformed.
    """


class AuthenticationError(HiveloomError):
    """Raised when a bearer token fails to authenticate.

    Covers a missing/malformed ``Authorization`` header, an unknown key id,
    an invalid signature, an expired token, or a revoked key. Used by
    :mod:`hiveloom.serve.auth`; the control-plane server maps this to HTTP 401.
    Distinct from :class:`AuthorizationError` below.
    """


class AuthorizationError(HiveloomError):
    """Raised when an authenticated bearer token lacks a required scope.

    A caller can present a validly-signed, non-revoked token and still be
    refused here if neither its authorized key's scopes nor its own
    ``scope`` claim cover what was requested. Kept distinct from
    :class:`AuthenticationError` because the control-plane server maps this to
    HTTP 403 versus 401 for authentication failures.
    """


class NotFoundError(HiveloomError):
    """Raised when an HTTP-addressable resource (a run, a proposal) doesn't exist.

    Distinct from :class:`SpecError` (a caller mistake, mapped to 400): this
    is "the id you asked for isn't there", mapped by the control plane to 404.
    """


class McpError(HiveloomError):
    """Raised when a declared MCP server is unavailable or misbehaves at runtime.

    The spec reference validated fine; this is a runtime failure (exit 4) —
    connecting to the declared server, or the process/network behind it,
    failed.
    """
