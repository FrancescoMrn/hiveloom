"""The harness's fetch tool, exposed to inspect_ai for the raw arms.

Imports the canonical implementation — any behavioral difference in the tool
would confound the harness-vs-raw comparison.
"""

from __future__ import annotations

import asyncio

from inspect_ai.tool import tool

from inspect_evals._shared import load_digest_fn

_digest = load_digest_fn()


@tool
def fetch_clean():
    async def execute(url: str) -> str:
        """HTTP GET a web page and return a compact deterministic digest: TITLE /
        META / TIME / H1 / H2 / H3 / LEAD TEXT lines, always under 8KB. On failure
        returns a line starting with 'ERROR:'.

        Args:
            url: The URL to fetch.
        """
        return await asyncio.to_thread(_digest, url)

    return execute
