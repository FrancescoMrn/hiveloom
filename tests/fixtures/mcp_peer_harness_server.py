"""A stdio MCP server that exposes ONE hiveloom harness (harness-to-harness).

Spawned as a real subprocess by tests/test_mcp_serve.py
(``command=sys.executable, args=[this file, <harness dir>, <scripted output>]``)
so the delegation path — request ``_meta`` lineage, the Hive link, the
depth/cycle guard, errors as data — is exercised over a real wire rather than
in process.

The model is faked here, not in the parent: the peer harness runs inside THIS
process, so its provider must be scripted here too. Everything else (trust
gate, spec validation, runner, Hive ingest) is the real thing.
"""

from __future__ import annotations

import sys

from hiveloom.models.fake import FakeModelProvider, text_response
from hiveloom.serve.mcp import build_mcp_server


def main() -> None:
    harness_dir = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else "peer answered"
    max_depth = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    server = build_mcp_server(
        [harness_dir],
        provider_factory=lambda: FakeModelProvider([text_response(output)]),
        max_depth=max_depth,
    )
    server.run("stdio")


if __name__ == "__main__":
    main()
