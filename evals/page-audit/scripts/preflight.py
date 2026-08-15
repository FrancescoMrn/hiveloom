#!/usr/bin/env python3
"""Calibration: for each candidate URL, compare what fetch_clean's digest
shows (capped at 30 headings / 7500 chars) against the uncapped page truth,
so we know which samples actually stress exhaustiveness."""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "validators-src"))
import page_audit as pa  # noqa: E402

TOOL = ROOT / ".." / ".." / "harnesses" / "article-extractor" / "tools" / "fetch_clean.py"
spec = importlib.util.spec_from_file_location("fetch_clean", TOOL)
fc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(fc)
except ImportError:
    fc = None  # hiveloom decorator import may fail outside the venv

CANDIDATES = {
    "s09": "https://docs.python.org/3/library/re.html",
    "s15": "https://www.gnu.org/software/bash/manual/bash.html",
    "s31": "https://go.dev/doc/effective_go",
    "s14": "https://www.postgresql.org/docs/current/sql-select.html",
    "s01": "https://www.phoronix.com/news/Linux-6.10-Released",
    "s22": "https://blog.cloudflare.com/pq-2024/",
    "s25": "https://jvns.ca/blog/2021/04/03/what-problems-do-people-solve-with-strace/",
}

for sid, url in CANDIDATES.items():
    try:
        truth = pa.page_truth(url)
    except OSError as exc:
        print(f"{sid}: FETCH FAILED {exc}")
        continue
    digest = fc._digest(url) if fc else ""
    dig_h2 = len(re.findall(r"^H2: ", digest, re.M))
    trunc = "TRUNCATED" if dig_h2 < len(truth["h2"]) else "complete"
    print(
        f"{sid}: true_h2={len(truth['h2'])} digest_h2={dig_h2} {trunc} "
        f"digest_len={len(digest)} pub_dates={sorted(truth['dates'])[:4]}"
    )
