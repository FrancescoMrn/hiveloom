#!/usr/bin/env python
"""Aggregate inspect_ai .eval logs from all arms into RESULTS.md.

Usage: python scripts/aggregate_results.py logs/haiku_harness logs/haiku_raw ... --out RESULTS.md
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inspect_ai.log import read_eval_log

from inspect_evals._shared import EVAL_ROOT, PRICING_AS_OF, PRICING_PER_MTOK, wilson_ci

SCORER_NAME = "article_extractor_scorer"


def load_arm(log_dir: Path) -> dict:
    eval_files = sorted(log_dir.glob("*.eval"))
    if not eval_files:
        sys.exit(f"no .eval log in {log_dir}")
    log = read_eval_log(str(eval_files[-1]))

    rows = []
    for sample in log.samples or []:
        score = (sample.scores or {}).get(SCORER_NAME)
        if score is None:
            continue
        meta = score.metadata or {}
        rows.append(
            {
                "id": sample.id,
                "epoch": sample.epoch,
                "success": score.value == "C",
                "meta": meta,
                "cost": _sample_cost(sample, meta),
            }
        )
    unpriced = sum(1 for r in rows if r["cost"] is None)
    if unpriced:
        print(
            f"WARNING: {log_dir.name}: {unpriced}/{len(rows)} rows have no cost "
            "(model not in PRICING_PER_MTOK or hiveloom timeout) — cost columns "
            "cover priced rows only",
            file=sys.stderr,
        )
    return {"rows": rows, "log": str(eval_files[-1]), "unpriced": unpriced}


def _sample_cost(sample, meta) -> float | None:
    hiveloom = meta.get("hiveloom_result")
    if hiveloom is not None:
        return hiveloom.get("cost_usd")
    total = 0.0
    for model_name, usage in (sample.model_usage or {}).items():
        bare = model_name.split("/")[-1]
        # Prefix match tolerates dated suffixes on the served model id.
        prices = next((p for k, p in PRICING_PER_MTOK.items() if bare.startswith(k)), None)
        if prices is None:
            return None
        in_price, out_price = prices
        # inspect's anthropic provider prompt-caches the system prompt;
        # usage.input_tokens is the uncached remainder only. Cache writes bill
        # at 1.25x input, reads at 0.1x.
        total += (
            usage.input_tokens / 1e6 * in_price
            + (usage.input_tokens_cache_write or 0) / 1e6 * in_price * 1.25
            + (usage.input_tokens_cache_read or 0) / 1e6 * in_price * 0.1
            + usage.output_tokens / 1e6 * out_price
        )
    return total


def _pct(values: list[bool]) -> str:
    return f"{100 * sum(values) / len(values):.0f}%" if values else "—"


def _mean_of(rows, key) -> str:
    vals = [r["meta"][key] for r in rows if key in r["meta"]]
    return f"{statistics.mean(vals):.2f}" if vals else "—"


def summarize(name: str, arm: dict) -> dict:
    rows = arm["rows"]
    n = len(rows)
    successes = sum(r["success"] for r in rows)
    lo, hi = wilson_ci(successes, n)
    costs = [r["cost"] for r in rows if r["cost"] is not None]
    latencies = sorted(
        r["meta"]["latency_seconds"]
        for r in rows
        if isinstance(r["meta"].get("latency_seconds"), (int, float))
    )
    # cost_per_success is total spend / successes; only honest when every row
    # is priced, so flag it when some rows had no cost.
    total_cost = sum(costs)
    flag = "*" if arm["unpriced"] else ""

    by_id: dict[str, list[bool]] = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r["success"])
    pass_all = [all(v) for v in by_id.values()]

    halluc = [not r["meta"]["hallucination_passed"] for r in rows if "hallucination_passed" in r["meta"]]

    def q(p: float) -> str:
        if not latencies:
            return "—"
        return f"{latencies[min(int(p * len(latencies)), len(latencies) - 1)]:.1f}"

    return {
        "arm": name,
        "n": n,
        "success": f"{100 * successes / n:.0f}% ({100 * lo:.0f}–{100 * hi:.0f})" if n else "—",
        "halluc": _pct(halluc),
        "title": _pct([r["meta"]["title_match"] for r in rows if "title_match" in r["meta"]]),
        "author": _pct([r["meta"]["author_match"] for r in rows if "author_match" in r["meta"]]),
        "date": _pct([r["meta"]["date_match"] for r in rows if "date_match" in r["meta"]]),
        "headings_f1": _mean_of(rows, "headings_f1"),
        "mean_cost": f"${statistics.mean(costs):.4f}{flag}" if costs else "—",
        "cost_per_success": f"${total_cost / successes:.4f}{flag}" if successes and costs else "—",
        "latency": f"{q(0.5)} / {q(0.9)}",
        "pass_all": _pct(pass_all),
    }


def mcnemar(arm_a: dict, arm_b: dict) -> str:
    from scipy.stats import binomtest

    outcomes_a = {(r["id"], r["epoch"]): r["success"] for r in arm_a["rows"]}
    outcomes_b = {(r["id"], r["epoch"]): r["success"] for r in arm_b["rows"]}
    keys = outcomes_a.keys() & outcomes_b.keys()
    b = sum(1 for k in keys if outcomes_a[k] and not outcomes_b[k])
    c = sum(1 for k in keys if not outcomes_a[k] and outcomes_b[k])
    if b + c == 0:
        return f"no discordant pairs over {len(keys)} paired runs — arms identical on task_success"
    p = binomtest(min(b, c), b + c, 0.5).pvalue
    return (
        f"{len(keys)} paired runs; harness-only wins b={b}, raw-only wins c={c}; "
        f"exact McNemar p={p:.4f}"
    )


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=EVAL_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        return out or "uncommitted"
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log_dirs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=EVAL_ROOT / "RESULTS.md")
    args = parser.parse_args()

    arms = {d.name: load_arm(d) for d in args.log_dirs}
    summaries = [summarize(name, arm) for name, arm in arms.items()]

    cols = [
        ("Arm", "arm"), ("n", "n"), ("Task success (95% CI)", "success"),
        ("Halluc.", "halluc"), ("Title", "title"), ("Author", "author"), ("Date", "date"),
        ("Headings F1", "headings_f1"), ("Mean cost", "mean_cost"),
        ("Cost/success", "cost_per_success"), ("p50/p90 lat (s)", "latency"), ("pass^k", "pass_all"),
    ]
    lines = [
        "# Results: article-extractor benchmark",
        "",
        f"Generated {date.today().isoformat()} · dataset "
        f"`{_git(['log', '-1', '--format=%h', '--', 'dataset/samples.jsonl'])}` · "
        f"repo `{_git(['rev-parse', '--short', 'HEAD'])}`",
        "",
        "| " + " | ".join(c[0] for c in cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for s in summaries:
        lines.append("| " + " | ".join(str(s[c[1]]) for c in cols) + " |")

    lines += ["", "## Paired comparison", ""]
    if "haiku_harness" in arms and "sonnet_raw" in arms:
        lines.append(f"**haiku_harness vs sonnet_raw**: {mcnemar(arms['haiku_harness'], arms['sonnet_raw'])}")
    if "haiku_harness" in arms and "haiku_raw" in arms:
        lines.append("")
        lines.append(f"**haiku_harness vs haiku_raw** (harness contribution): {mcnemar(arms['haiku_harness'], arms['haiku_raw'])}")

    lines += [
        "",
        "## Caveats",
        "",
        f"- Pricing (USD/Mtok, as of {PRICING_AS_OF}): "
        + ", ".join(f"{m} {p}" for m, p in PRICING_PER_MTOK.items())
        + ". Sonnet 5 is at the introductory rate through 2026-08-31.",
        "- qwen arm cost is ~$0 by hiveloom accounting (local inference, usage-less "
        "openai-compat responses counted free) — latency is its resource proxy.",
        "- The scorer strips one markdown fence layer for all arms (lenient to raw arms).",
        "- Harness-arm cost comes from hiveloom's own accounting; raw-arm cost from "
        "inspect_ai token usage × the pricing table (cache writes at 1.25x input, reads at 0.1x).",
        "- A `*` on a cost cell means some rows in that arm had no price "
        "(unpriced model or timeout) and are excluded from the cost math.",
        "- Live-URL dataset: see the drift check run alongside this sweep.",
        "",
        "Logs: " + ", ".join(f"`{arm['log']}`" for arm in arms.values()),
    ]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
