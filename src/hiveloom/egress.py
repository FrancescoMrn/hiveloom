"""The last check before content leaves the machine for a model provider.

Everything else in the runtime is about what a tool may *read*. This is about
what may leave: the exact request handed to :meth:`ModelProvider.complete` is
inspected first, and matched secrets are redacted or the request is refused.

It is defence in depth, not the primary control. Pattern matching cannot
recognise arbitrary sensitive text — a previous run's customer data is not
shaped like a credential — which is why path isolation
(:mod:`hiveloom.private`, :mod:`hiveloom.confine`) stays the boundary that
actually closes the exfiltration path. What this catches is the narrow, common
case that isolation cannot: a credential that reached the conversation through
a legitimate read, a hook, or the task input itself.

Two sources of patterns:

* ``logging.redact`` — the harness's own patterns. They already scrub the
  journal; applying them here too closes the gap where a secret was persisted
  as ``[REDACTED]`` and still sent verbatim to the provider.
* :data:`CREDENTIAL_PATTERNS` — well-known credential shapes, on by default.

Findings are reported as pattern name and count. The matched text is never
journalled, never logged, and never included in an error message: a safeguard
that records what it caught would be the leak it exists to prevent.

``egress`` is frozen from evolution, like the guardrails and the redact list it
extends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.spec.schema import EgressConfig

REDACTION = "[REDACTED]"

#: Credential shapes recognised by default. Anchored on issuer-specific
#: prefixes and lengths rather than on entropy: a heuristic that fires on
#: ordinary text would train operators to switch the check off.
CREDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private_key_block", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ("aws_access_key_id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("anthropic_api_key", r"\bsk-ant-[A-Za-z0-9_\-]{24,}"),
    ("openai_api_key", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("bearer_header", r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{16,}"),
)


@dataclass(frozen=True)
class EgressVerdict:
    """What the filter did to one outgoing request."""

    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    blocked: bool = False

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        """Pattern names and counts — never the text that matched."""
        return ", ".join(f"{f['pattern']}×{f['count']}" for f in self.findings)


class EgressFilter:
    """Applies an :class:`EgressConfig` to outgoing provider requests."""

    def __init__(self, config: EgressConfig, redact_patterns: list[str] | None = None):
        self._config = config
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for index, pattern in enumerate(redact_patterns or []):
            with_name = f"logging.redact[{index}]"
            self._patterns.append((with_name, re.compile(pattern, re.IGNORECASE)))
        for index, pattern in enumerate(config.patterns):
            self._patterns.append((f"egress.patterns[{index}]", re.compile(pattern)))
        if config.detect_credentials:
            self._patterns += [(name, re.compile(p)) for name, p in CREDENTIAL_PATTERNS]

    @property
    def enabled(self) -> bool:
        return self._config.mode != "off" and bool(self._patterns)

    def apply(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> EgressVerdict:
        """Scan a request, returning the version that may leave (or a block).

        The request is rewritten as a *copy*: history keeps what actually
        happened, and the model is what is protected from carrying the secret
        onward. Redaction is idempotent, so a match that survives in history is
        scrubbed again on every later turn.
        """
        outgoing_tools = tools if tools is not None else []
        if not self.enabled:
            return EgressVerdict(
                system=system, messages=messages, tools=outgoing_tools, findings=[]
            )

        counts: dict[str, int] = {}

        def scrub(text: str) -> str:
            for name, pattern in self._patterns:
                text, hits = pattern.subn(REDACTION, text)
                if hits:
                    counts[name] = counts.get(name, 0) + hits
            return text

        cleaned_system = scrub(system)
        cleaned_messages = [_map_text(message, scrub) for message in messages]
        cleaned_tools = [_map_text(tool, scrub) for tool in outgoing_tools]
        findings = [{"pattern": name, "count": count} for name, count in sorted(counts.items())]
        if not findings:
            return EgressVerdict(
                system=system, messages=messages, tools=outgoing_tools, findings=[]
            )
        if self._config.mode == "block":
            return EgressVerdict(
                system=system,
                messages=messages,
                tools=outgoing_tools,
                findings=findings,
                blocked=True,
            )
        return EgressVerdict(
            system=cleaned_system,
            messages=cleaned_messages,
            tools=cleaned_tools,
            findings=findings,
        )


def _map_text(value: Any, scrub) -> Any:
    """Rewrite every string in a message tree, leaving its shape untouched."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _map_text(item, scrub) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_text(item, scrub) for item in value]
    return value


def policy_name(config: EgressConfig) -> str:
    """A one-word description of the policy, for ``hiveloom confinement``."""
    if config.mode == "off":
        return "off"
    return "block" if config.mode == "block" else "redact"
