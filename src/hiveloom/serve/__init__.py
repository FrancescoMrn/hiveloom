"""The hiveloom HTTP control plane: identity, auth, and (later) the server itself.

This package currently holds only the identity foundation — key generation and
bearer-token auth (see ``keys.py``/``auth.py``). It builds no HTTP server; that
arrives in the next task, alongside ``docs/control-plane.md``.
"""

from __future__ import annotations
