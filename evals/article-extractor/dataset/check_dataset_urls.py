#!/usr/bin/env python
"""Pre-flight dataset check: re-fetch every sample URL and report drift.

Warns, never fails — a drifted or dead URL needs human triage, not a hard stop.
Run before (and after) every full sweep.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inspect_evals._shared import DATASET_PATH, derive_title_headings, fingerprint, load_digest_fn

_digest = load_digest_fn()


def check_row(row: dict) -> tuple[str, str]:
    if row["url"] != row["golden"]["source_url"]:
        return "BAD", "url != golden.source_url"
    expect_dead = row["golden"]["title"] is None
    digest = _digest(row["url"])
    if digest.startswith("ERROR:"):
        if expect_dead:
            return "OK", "dead as expected (404 edge case)"
        return "DEAD", digest[:100]
    if expect_dead:
        return "DRIFT", "expected-404 URL is now fetchable"
    title, headings = derive_title_headings(digest)
    live = fingerprint(title, headings)
    stored = row["fingerprint"]
    diffs = [k for k in stored if stored[k] != live[k]]
    if diffs:
        return "DRIFT", ", ".join(f"{k}: {stored[k]} -> {live[k]}" for k in diffs)
    return "OK", ""


def main() -> None:
    rows = [json.loads(line) for line in DATASET_PATH.read_text().splitlines() if line.strip()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check_row, rows))

    print(f"{'id':<5} {'category':<10} {'status':<7} detail")
    problems = 0
    for row, (status, detail) in zip(rows, results, strict=True):
        problems += status != "OK"
        print(f"{row['id']:<5} {row['category']:<10} {status:<7} {detail}")

    print(f"\n{len(rows)} samples checked, {problems} need triage.")
    if problems:
        print("Drifted samples: re-verify the golden against the live page or replace the URL.")


if __name__ == "__main__":
    main()
