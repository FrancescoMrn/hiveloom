"""Shared constants and helpers: canonical-harness loading, pricing, stats.

The canonical harness at the repo root is the single source of truth for the
system prompt, the fetch tool, the output schema, and the validator. Everything
here loads from it so the eval never drifts from what hiveloom actually ships.
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import re
import sys
import types
from pathlib import Path

import yaml

EVAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_HARNESS = REPO_ROOT / "harnesses" / "article-extractor"
DATASET_PATH = EVAL_ROOT / "dataset" / "samples.jsonl"

# Anthropic first-party rates, USD per 1M tokens (input, output).
# Sonnet 5 is at the introductory rate through 2026-08-31; standard is (3.00, 15.00).
# Local Ollama models are $0 marginal cost by definition; latency is their proxy.
PRICING_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "qwen3:4b-instruct": (0.0, 0.0),
    "gemma4:12b-mlx": (0.0, 0.0),
    "Qwen3.6-35B-A3B-8bit": (0.0, 0.0),
}
PRICING_AS_OF = "2026-07-29"


def load_harness_system_prompt() -> str:
    spec = yaml.safe_load((CANONICAL_HARNESS / "harness.yaml").read_text())
    return spec["system_prompt"]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_digest_fn():
    """Return the canonical ``_digest(url) -> str`` from the harness's fetch tool.

    The tool module does ``from hiveloom.tools import tool`` at import time;
    hiveloom is deliberately not a dependency of this project, so stub the
    decorator when it isn't importable.
    """
    try:
        import hiveloom.tools  # noqa: F401
    except ImportError:
        tools_mod = types.ModuleType("hiveloom.tools")

        def tool(*_args, **_kwargs):
            def deco(fn):
                return fn

            return deco

        tools_mod.tool = tool
        pkg = sys.modules.setdefault("hiveloom", types.ModuleType("hiveloom"))
        pkg.tools = tools_mod
        sys.modules["hiveloom.tools"] = tools_mod
    mod = _load_module("canonical_fetch_clean", CANONICAL_HARNESS / "tools" / "fetch_clean.py")
    return mod._digest


def load_page_validator():
    """Return the canonical ``validate(run_output, run_context)`` anti-hallucination check."""
    mod = _load_module(
        "canonical_article_on_page", CANONICAL_HARNESS / "validators" / "article_on_page.py"
    )
    return mod.validate


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def derive_title_headings(digest: str) -> tuple[str | None, list[str]]:
    """Apply the harness prompt's deterministic BUILD rules for title and headings."""
    title = og_title = first_h1 = None
    headings: list[str] = []
    for line in digest.splitlines():
        if title is None and line.startswith("TITLE: "):
            title = line[len("TITLE: ") :]
        elif og_title is None and line.startswith("META og:title: "):
            og_title = line[len("META og:title: ") :]
        elif line[:4] in ("H1: ", "H2: ", "H3: "):
            headings.append(line[4:])
            if first_h1 is None and line.startswith("H1: "):
                first_h1 = line[4:]
    return (title or og_title or first_h1), headings[:15]


def fingerprint(title: str | None, headings: list[str]) -> dict:
    joined = "\n".join(norm(h) for h in headings)
    return {
        "title_hash": hashlib.sha256(norm(title or "").encode()).hexdigest()[:12],
        "heading_count": len(headings),
        "heading_hash": hashlib.sha256(joined.encode()).hexdigest()[:12],
    }


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval. Correct only when the n observations are independent.

    Not what you want for this benchmark's arms: see :func:`cluster_bootstrap_ci`.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = p + z**2 / (2 * n)
    margin = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5)
    return (max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom))


def cluster_bootstrap_ci(
    clusters: list[list[bool]], *, resamples: int = 10000, seed: int = 0
) -> tuple[float, float]:
    """95% CI for a success rate when observations come in correlated clusters.

    Each arm runs the same URL over several epochs, so the rows are not
    independent: a hard page tends to fail every epoch. Treating them as
    independent shrinks the interval and inflates significance. Resampling
    whole URLs with replacement keeps the within-URL correlation intact.

    Seeded so a regenerated RESULTS.md does not churn on noise.
    """
    if not clusters:
        return (0.0, 0.0)
    k = len(clusters)
    # When every cluster has the same rate, every resample is identical and the
    # bootstrap collapses to a point, claiming certainty it has not earned. That
    # is not only the all-pass/all-fail case: uniform 0.5 degenerates too. Fall
    # back to Wilson over the clusters, which still widens when there are few.
    cluster_rates = {sum(c) / len(c) for c in clusters}
    if len(cluster_rates) == 1:
        return wilson_ci(round(next(iter(cluster_rates)) * k), k)

    rng = random.Random(seed)
    rates = []
    for _ in range(resamples):
        drawn = [clusters[rng.randrange(k)] for _ in range(k)]
        flat = [ok for c in drawn for ok in c]
        rates.append(sum(flat) / len(flat))
    rates.sort()
    return (rates[int(0.025 * resamples)], rates[int(0.975 * resamples)])
