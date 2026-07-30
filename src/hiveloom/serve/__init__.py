"""Serving a harness over HTTP: the simple deployment server and the control plane.

Two distinct surfaces live here:

- ``simple.py`` — :class:`HarnessServer`, the stdlib HTTP server behind
  ``hiveloom serve`` (and the docker ``--serve`` entrypoint): ``GET /healthz``
  plus ``POST /runs``, optional ``HIVELOOM_API_KEY`` bearer auth.
- ``app.py``/``keys.py``/``auth.py``/``runslots.py`` — the bearer-authorized
  control plane behind ``hiveloom control-plane``: a scoped operational subset
  of the CLI over HTTP. See ``docs/control-plane.md``.
"""

from __future__ import annotations

from hiveloom.serve.simple import HarnessServer, serve_forever

__all__ = ["HarnessServer", "serve_forever"]
