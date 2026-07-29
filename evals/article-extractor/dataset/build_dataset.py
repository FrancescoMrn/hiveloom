#!/usr/bin/env python
"""Golden-authoring helper: print the digest and a draft samples.jsonl line for a URL.

Usage: python dataset/build_dataset.py <url> [<id>] [<category>]

The deterministic fields (title, headings, source_url, description, fingerprint)
are filled from the digest per the harness prompt's BUILD rules; author and
published_date are drafted from META lines only when unambiguous. REVIEW THE
DRAFT AGAINST THE LIVE PAGE before appending it to samples.jsonl.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inspect_evals._shared import derive_title_headings, fingerprint, load_digest_fn


def draft(url: str, sample_id: str, category: str) -> dict:
    digest = load_digest_fn()(url)
    print(digest, end="\n\n---\n\n", file=sys.stderr)
    if digest.startswith("ERROR:"):
        golden = {
            "source_url": url,
            "title": None,
            "description": None,
            "author": None,
            "published_date": None,
            "headings": [],
        }
        fp = fingerprint(None, [])
    else:
        title, headings = derive_title_headings(digest)
        meta = {}
        for line in digest.splitlines():
            if line.startswith("META "):
                key, _, value = line[len("META ") :].partition(": ")
                meta.setdefault(key, value)
        date = (meta.get("article:published_time") or "")[:10]
        golden = {
            "source_url": url,
            "title": title,
            "description": meta.get("description") or meta.get("og:description"),
            "author": meta.get("author") or meta.get("article:author"),
            "published_date": date if len(date) == 10 and date[4] == "-" else None,
            "headings": headings,
        }
        fp = fingerprint(title, headings)
    return {
        "id": sample_id,
        "url": url,
        "category": category,
        "golden": golden,
        "fingerprint": fp,
        "verified_at": "REVIEW-ME",
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url = sys.argv[1]
    sample_id = sys.argv[2] if len(sys.argv) > 2 else "sXX"
    category = sys.argv[3] if len(sys.argv) > 3 else "REVIEW-ME"
    print(json.dumps(draft(url, sample_id, category), ensure_ascii=False))
