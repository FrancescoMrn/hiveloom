#!/usr/bin/env python3
"""Score article-digest results and write RESULTS.md.

Every arm is scored with the SAME deterministic checks (the harness
validator's own `check` function), applied outside hiveloom, so harness and
raw arms are judged identically. One markdown fence layer is stripped for all
arms (lenient to raw arms, mirroring the article-extractor scorer).

Each URL's live page is fetched once and cached for the whole scoring pass.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "digest_on_page", ROOT / "validators-src" / "digest_on_page.py"
)
dv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dv)


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
    results_path = ROOT / "results" / "results.jsonl"
    records = [json.loads(l) for l in results_path.read_text().splitlines()]
    if not records:
        sys.exit("no results to score")

    pages: dict[str, str] = {}

    def page_for(url: str) -> str | None:
        if url not in pages:
            try:
                pages[url] = dv._page_text(url)
            except OSError as exc:
                print(f"warning: could not fetch {url}: {exc}", file=sys.stderr)
                pages[url] = ""
        return pages[url] or None

    scored = []
    for rec in records:
        row = dict(rec)
        row.update(
            success=False, parsed=False, problems=["run failed"], stats={},
            output_words=0,
        )
        out = strip_fence(rec.get("output") or "")
        row["output_words"] = len(out.split())
        if out:
            try:
                data = json.loads(out)
                row["parsed"] = True
            except (json.JSONDecodeError, ValueError):
                data = None
                row["problems"] = ["output is not valid JSON"]
            if data is not None:
                page = page_for(rec["url"])
                if page is None:
                    row["problems"] = ["page unfetchable at scoring time"]
                else:
                    problems, stats = dv.check(data, rec["url"], page)
                    row["problems"] = problems
                    row["stats"] = stats
                    row["success"] = not problems
        scored.append(row)

    arms = sorted({r["arm"] for r in scored})
    lines = [
        "# Results: article-digest (Opus 5 / Sonnet 5, output-heavy task)",
        "",
        "All arms scored with the identical deterministic checks "
        "(`validators-src/digest_on_page.py:check`), one fence layer stripped "
        "for every arm. Success = source_url exact AND summary 100-260 original "
        "words (not verbatim page text) AND 5 distinct verbatim quotes "
        "(<=1 missing tolerated) AND outline verbatim (<=1 missing tolerated).",
        "",
        "| Arm | n | Success | Halluc. quotes | Verbatim summary | Mean cost | "
        "Cost/success | p50 lat (s) | Mean output words |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        rows = [r for r in scored if r["arm"] == arm]
        n = len(rows)
        succ = sum(r["success"] for r in rows)
        halluc = sum(
            1 for r in rows if r["stats"] and r["stats"].get("missing_quotes", 0) > 1
        )
        verb = sum(1 for r in rows if r["stats"] and r["stats"].get("summary_verbatim"))
        costs = [r.get("cost_usd") or 0.0 for r in rows]
        mean_cost = sum(costs) / n
        cost_per_success = (sum(costs) / succ) if succ else float("nan")
        lats = sorted(r.get("duration_seconds") or r.get("wall_seconds") or 0 for r in rows)
        p50 = statistics.median(lats) if lats else 0
        words = [r["output_words"] for r in rows]
        lines.append(
            f"| {arm} | {n} | {succ / n:.0%} | {halluc / n:.0%} | {verb / n:.0%} | "
            f"${mean_cost:.4f} | ${cost_per_success:.4f} | {p50:.1f} | "
            f"{sum(words) / n:.0f} |"
        )

    lines += ["", "## Failures by arm", ""]
    for arm in arms:
        fails = [r for r in scored if r["arm"] == arm and not r["success"]]
        if not fails:
            continue
        lines.append(f"### {arm}")
        for r in fails:
            lines.append(
                f"- {r['sample_id']} e{r['epoch']}: "
                + "; ".join(r["problems"])[:300]
            )
        lines.append("")

    out_md = ROOT / "RESULTS.md"
    out_md.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {out_md}")


if __name__ == "__main__":
    main()
