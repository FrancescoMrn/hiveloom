"""Tools and the public surface used by code hooks.

``@tool`` marks a function as a tool. A tool normally returns a string (or any
value that renders as one), which becomes what the model reads. Returning a
:class:`~hiveloom.tools.registry.ToolResult` instead lets a tool also attach
:class:`~hiveloom.tools.registry.Artifact` side-products for the embedding
application — a chart spec, a decision proposal — without pushing them through
the model's text channel.
"""

from typing import TYPE_CHECKING, Any

from hiveloom.tools.decorators import tool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hiveloom.tools.registry import Artifact, ToolResult  # noqa: F401

_LAZY = {"Artifact": "Artifact", "ToolResult": "ToolResult"}


def __getattr__(name: str) -> Any:
    """Lazily re-export from ``registry`` (it imports the spec layer)."""
    if name in _LAZY:
        from hiveloom.tools import registry

        value = getattr(registry, _LAZY[name])
        globals()[name] = value
        return value
    raise AttributeError(f"module 'hiveloom.tools' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


__all__ = ["Artifact", "ToolResult", "tool"]
