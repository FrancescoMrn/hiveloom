"""The ``@tool`` decorator for user code hooks.

It attaches metadata and returns the original function unchanged, so
``inspect.signature`` (used by the loader's hook resolution) still sees the
real signature. The registry builds its provider-neutral tool payload from the
attached metadata plus the function's type hints.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def tool(
    func: Callable[..., Any] | None = None,
    *,
    description: str | None = None,
    tags: list[str] | None = None,
    guidelines: str | None = None,
    handles: list[str] | None = None,
) -> Callable[..., Any]:
    """Mark a function as a hiveloom tool.

    Usage::

        @tool(description="Fetch a PO record by number.", tags=["read"])
        def fetch_po(po_number: str) -> dict: ...

    Can be used bare (``@tool``) or called (``@tool(...)``). ``guidelines``
    is short usage guidance injected into the system prompt while the tool is
    active (name the tool in it).

    ``handles`` names string parameters that may be given the handle of a
    spilled tool result instead of a literal::

        @tool(description="Index a document.", handles=["text"])
        def index(text: str) -> str: ...

    The runtime then replaces a handle-shaped value with the stored object's
    full text at dispatch, so a large result can move from one tool to another
    without passing through the model's context. A value that is not a handle
    is passed through unchanged, and an unresolvable handle is a tool error
    rather than a literal.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__hiveloom_tool__ = {  # type: ignore[attr-defined]
            "description": description or (fn.__doc__ or "").strip(),
            "tags": list(tags or []),
            "guidelines": (guidelines or "").strip(),
            "handles": list(handles or []),
        }
        return fn

    if func is not None:
        return decorate(func)
    return decorate
