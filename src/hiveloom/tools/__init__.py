"""Tool package. The registry and builtins land in M2; the ``@tool`` decorator
lives here now because scaffolded code hooks import it and the loader resolves
against it.
"""

from hiveloom.tools.decorators import tool

__all__ = ["tool"]
