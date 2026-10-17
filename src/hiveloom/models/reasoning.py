"""One reasoning setting, mapped to the field each host actually reads.

``model.reasoning`` is a single provider-neutral level. The request field that
carries it differs per host, and a host that does not recognise the field
usually ignores it without an error — so the mapping is explicit and keyed by a
*reasoning style*, never guessed from the URL:

``openai``
    ``reasoning_effort`` (OpenAI and the hosts that copied it).
``openrouter``
    A ``reasoning`` object: ``{"effort": level}``, or ``{"enabled": false}``.
``chat_template``
    ``reasoning_effort``, plus ``chat_template_kwargs.enable_thinking: false``
    to turn thinking off — vLLM/SGLang-style servers that render a chat
    template, where the effort field alone is not always honoured.
``none``
    The host has no reasoning control. A non-default level is an error rather
    than a setting that quietly does nothing.

``anthropic`` is the Claude provider's own mapping (:func:`claude_reasoning_fields`).
"""

from __future__ import annotations

from typing import Any, Literal

ReasoningLevel = Literal["default", "none", "minimal", "low", "medium", "high"]

#: Styles a ``models.yaml`` entry or ``register_provider`` may declare.
REASONING_STYLES = ("openai", "openrouter", "chat_template", "none")
#: Where a host that declares nothing lands: the most widely copied field.
DEFAULT_REASONING_STYLE = "openai"
#: The Claude provider's fixed style; not declarable for other hosts.
ANTHROPIC_REASONING_STYLE = "anthropic"

#: Request fields a style may write. They are what ``reasoning_sent`` records,
#: and what an explicit ``model.params`` entry overrides.
REASONING_REQUEST_KEYS = ("reasoning", "reasoning_effort", "chat_template_kwargs")


class ReasoningUnsupportedError(ValueError):
    """``model.reasoning`` asks for something this host or model cannot do."""


def openai_compat_reasoning_fields(style: str, level: str) -> dict[str, Any]:
    """The request fields that carry ``level`` for an OpenAI-compatible host."""
    if level == "default":
        return {}
    if style == "openai":
        return {"reasoning_effort": level}
    if style == "openrouter":
        if level == "none":
            return {"reasoning": {"enabled": False}}
        return {"reasoning": {"effort": level}}
    if style == "chat_template":
        fields: dict[str, Any] = {"reasoning_effort": level}
        if level == "none":
            fields["chat_template_kwargs"] = {"enable_thinking": False}
        return fields
    if style == "none":
        raise ReasoningUnsupportedError(
            f"model.reasoning is '{level}' but this provider has no reasoning control. "
            "Set model.reasoning to 'default', pass the host's own field in "
            "model.params, or declare reasoning_style for the provider in models.yaml."
        )
    raise ReasoningUnsupportedError(f"unknown reasoning_style '{style}'")


# Documented as rejecting `output_config.effort`. Any other id gets adaptive
# thinking plus effort; a model that turns out not to take them fails at the API.
_CLAUDE_NO_EFFORT_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5")
# Thinking is always on: the API rejects any way of turning it off.
_CLAUDE_ALWAYS_THINKING_PREFIXES = ("claude-opus-5-5", "claude-fable-5", "claude-mythos")
# Sonnet 5.5 rejects `disabled`; `between_tools` is its off switch.
_CLAUDE_BETWEEN_TOOLS_PREFIXES = ("claude-sonnet-5-5",)


def claude_reasoning_fields(model_id: str, level: str) -> dict[str, Any]:
    """The request fields that carry ``level`` for one Claude model.

    ``minimal`` has no Claude equivalent and is sent as ``low``; the record of
    what was sent says so.
    """
    if level == "default":
        return {}
    if level == "none":
        if model_id.startswith(_CLAUDE_ALWAYS_THINKING_PREFIXES):
            raise ReasoningUnsupportedError(
                f"{model_id} cannot turn thinking off: its thinking is always on. "
                "Use model.reasoning 'low' to spend the least on it."
            )
        if model_id.startswith(_CLAUDE_BETWEEN_TOOLS_PREFIXES):
            return {"thinking": {"type": "between_tools"}}
        return {"thinking": {"type": "disabled"}}
    if model_id.startswith(_CLAUDE_NO_EFFORT_PREFIXES):
        raise ReasoningUnsupportedError(
            f"{model_id} has no effort control, so model.reasoning '{level}' cannot be "
            "applied. Use 'none' or 'default', or choose a model with adaptive thinking."
        )
    effort = "low" if level == "minimal" else level
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


def check_reasoning(style: str, level: str, model_id: str) -> None:
    """Raise :class:`ReasoningUnsupportedError` if ``level`` cannot be applied."""
    if style == ANTHROPIC_REASONING_STYLE:
        claude_reasoning_fields(model_id, level)
    else:
        openai_compat_reasoning_fields(style, level)
