#!/usr/bin/env python3
"""Score page-audit results and write RESULTS.md.

Every arm is scored with the same deterministic checks
(`validators-src/page_audit.py:check`) against a single cached ground-truth
fetch per URL. The headline distinction: a failed run that hiveloom FLAGGED
(exit code 1 / verify_failed) versus a failed run reported as success —
"silent wrong".
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location(
    "page_audit", ROOT.parent / "validators-src" / "page_audit.py"
)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)
ROOT = ROOT.parent


def strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def main() -> None:
    records = [
        json.loads(line)
        for line in (ROOT / "results" / "results.jsonl").read_text().splitlines()
    ]
    if not records:
        sys.exit("no results to score")

    truths: dict[str, dict] = {}

    def truth_for(url: str) -> dict | None:
        if url not in truths:
            try:
                truths[url] = pa.page_truth(url)
            except OSError as exc:
                print(f"warning: could not fetch {url}: {exc}", file=sys.stderr)
                truths[url] = {}
        return truths[url] or None

    scored = []
    for rec in records:
        row = dict(rec, success=False, parsed=False, problems=["run failed"], stats={})
        out = strip_fence(rec.get("output") or "")
        if out:
            try:
                data = json.loads(out)
                row["parsed"] = True
            except (json.JSONDecodeError, ValueError):
                data = None
                row["problems"] = ["output is not valid JSON"]
            if data is not None:
                truth = truth_for(rec["url"])
                if truth is None:
                    row["problems"] = ["page unfetchable at scoring time"]
                else:
                    problems, stats = pa.check(data, rec["url"], truth)
                    row["problems"], row["stats"] = problems, stats
                    row["success"] = not problems
        scored.append(row)

    arms = sorted({r["arm"] for r in scored})
    lines = [
        "# Results: page-audit (exhaustiveness + arithmetic, Opus 5 / Sonnet 5)",
        "",
        "The fetch tool clips its digest (30 headings / 7.5KB); three of the six "
        "pages have more H2s than the digest shows, so an exhaustive answer is "
        "impossible from the tool alone — the probe measures whether models "
        "admit, approximate, or fabricate, and whether the harness converts "
        "silent wrongness into an explicit failure signal. All arms scored by "
        "the same deterministic checks against an uncapped live re-fetch.",
        "",
        "| Arm | n | Success | Count exact | Fabricated H2s | Arith ok | "
        "Silent wrong | Flagged | Mean cost | p50 lat (s) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        rows = [r for r in scored if r["arm"] == arm]
        n = len(rows)
        succ = sum(r["success"] for r in rows)
        counted = [r for r in rows if r["stats"].get("count_reported") is not None]
        count_ok = sum(
            1 for r in counted if r["stats"]["count_reported"] == r["stats"]["count_true"]
        )
        fab = sum(1 for r in rows if r["stats"].get("fabricated_h2", 0) > 1)
        arith_rows = [r for r in rows if r["stats"].get("arith_ok") is not None]
        arith_ok = sum(1 for r in arith_rows if r["stats"]["arith_ok"])
        silent = sum(
            1 for r in rows if not r["success"] and r.get("status") == "success"
        )
        flagged = sum(
            1 for r in rows if not r["success"] and r.get("status") != "success"
        )
        costs = [r.get("cost_usd") or 0.0 for r in rows]
        lats = sorted(r.get("duration_seconds") or r.get("wall_seconds") or 0 for r in rows)
        lines.append(
            f"| {arm} | {n} | {succ}/{n} | {count_ok}/{len(counted)} | {fab}/{n} | "
            f"{arith_ok}/{len(arith_rows)} | **{silent}/{n}** | {flagged}/{n} | "
            f"${sum(costs) / n:.4f} | {statistics.median(lats):.1f} |"
        )

    lines += ["", "## Per-run detail", ""]
    for arm in arms:
        lines.append(f"### {arm}")
        for r in [x for x in scored if x["arm"] == arm]:
            s = r["stats"]
            verdict = "OK" if r["success"] else (
                "FLAGGED" if r.get("status") != "success" else "SILENT WRONG"
            )
            lines.append(
                f"- {r['sample_id']}: {verdict} — h2 {s.get('count_reported')}/"
                f"{s.get('count_true')} true, fabricated={s.get('fabricated_h2')}, "
                f"missing={s.get('missing_h2')}, date_ok={s.get('date_ok')}, "
                f"arith_ok={s.get('arith_ok')}"
                + ("" if r["success"] else " — " + "; ".join(r["problems"])[:220])
            )
        lines.append("")

    out_md = ROOT / "RESULTS.md"
    out_md.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {out_md}")


if __name__ == "__main__":
    main()
