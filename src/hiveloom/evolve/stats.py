"""Small exact statistics for deciding whether evidence says anything.

Everything here answers one of two questions evolution keeps having to ask:
*is this difference real at this sample size*, and *how big would a
difference have to be before this sample could show it*. Harness evaluations
are small — tens of runs, not thousands — so the tests are exact rather than
asymptotic, and the power helpers exist to say "this could not have been
detected" out loud instead of letting a noisy delta read as a result.

Pure Python on purpose: no scipy in a runtime dependency set for five
functions whose closed forms fit on a page.
"""

from __future__ import annotations

import math

# Two-sided alpha 0.05 and power 0.8, the conventional pair.
_Z_ALPHA = 1.959964
_Z_BETA = 0.841621


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p-value for the 2x2 table ``[[a, b], [c, d]]``.

    Sums the probability of every table with the same margins that is no more
    likely than the observed one (the standard two-sided definition), with a
    small relative tolerance so ties from floating point are not dropped.
    """
    if min(a, b, c, d) < 0:
        raise ValueError("table cells must be non-negative")
    row1, col1, n = a + b, a + c, a + b + c + d
    if n == 0:
        return 1.0
    low, high = max(0, col1 - (n - row1)), min(row1, col1)

    def log_p(x: int) -> float:
        return (
            _log_comb(col1, x) + _log_comb(n - col1, row1 - x) - _log_comb(n, row1)
        )

    observed = log_p(a)
    total = 0.0
    for x in range(low, high + 1):
        lp = log_p(x)
        if lp <= observed + 1e-7:
            total += math.exp(lp)
    return min(1.0, total)


def binomial_two_sided(k: int, n: int) -> float:
    """Exact two-sided sign test: ``k`` of ``n`` discordant pairs went one way.

    This is McNemar's exact test when the pairs are binary outcomes of the same
    case under two versions, and the sign test when they are the signs of
    paired numeric differences. Ties are the caller's to drop before calling.
    """
    if n <= 0:
        return 1.0
    k = min(k, n - k)
    tail = sum(math.exp(_log_comb(n, i) - n * math.log(2)) for i in range(k + 1))
    return min(1.0, 2 * tail)


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """False-discovery-rate q-values, in the input order.

    The signal locator tests every feature a harness has at once; without this
    correction, twenty features at p < 0.05 would produce a "significant" one
    by chance about as often as not.
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        index = order[rank - 1]
        running = min(running, p_values[index] * m / rank)
        q[index] = min(1.0, running)
    return q


def wilson_interval(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for a proportion ``k / n``."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = _Z_ALPHA**2
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = _Z_ALPHA * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _variance(p: float) -> float:
    # A rate observed at exactly 0 or 1 still has sampling variance; flooring
    # it keeps the power estimate from claiming a zero-width detectable change.
    p = min(max(p, 0.05), 0.95)
    return p * (1 - p)


def detectable_change(p: float, n_per_arm: int) -> float:
    """Smallest absolute change in a rate that ``n_per_arm`` runs per version
    would detect (two-sided alpha 0.05, power 0.8). ``1.0`` when n is zero."""
    if n_per_arm <= 0:
        return 1.0
    return min(1.0, (_Z_ALPHA + _Z_BETA) * math.sqrt(2 * _variance(p) / n_per_arm))


def runs_needed(p: float, change: float) -> int:
    """Runs per version needed to detect an absolute ``change`` in a rate ``p``."""
    if change <= 0:
        raise ValueError("change must be positive")
    return math.ceil((_Z_ALPHA + _Z_BETA) ** 2 * 2 * _variance(p) / change**2)
