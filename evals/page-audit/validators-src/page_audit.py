"""Deterministic validator for the page-audit task (exhaustiveness + arithmetic).

The model reports the page's TOTAL number of H2 headings, ALL H2 headings, the
published date, and the exact day count from that date to 2026-01-01. This
validator re-fetches the live page with the SAME parsing rules as the
``fetch_clean`` tool but WITHOUT its caps (30 headings / 7.5KB digest), so it
knows the ground truth the model may not have seen. Every check is rule-based:
no model calls, no randomness.

- ``h2_count`` must equal the true count; ``h2_headings`` must match the true
  list (<=1 mismatch tolerated for live-page drift) and be internally
  consistent with the reported count.
- ``published_date`` must be a date actually present on the page (meta/time);
  null is accepted only when the page carries no ``article:published_time``.
- ``days_to_2026`` must be the exact signed day count from the model's own
  ``published_date`` to 2026-01-01 — pure arithmetic, recomputed here.

``check`` is pure (no I/O); ``validate`` is the hiveloom entry point.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.request
from html.parser import HTMLParser
from typing import Any

_REFERENCE = _dt.date(2026, 1, 1)
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "nav", "footer"}
_DATE_META_KEYS = {
    "date",
    "article:published_time",
    "article:modified_time",
    "parsely-pub-date",
    "publish-date",
}
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _AuditParser(HTMLParser):
    """Same skip/heading semantics as fetch_clean's parser, uncapped."""

    def __init__(self) -> None:
        super().__init__()
        self.h2: list[str] = []
        self.meta_dates: dict[str, list[str]] = {}
        self.times: list[str] = []
        self._skip_depth = 0
        self._heading_tag: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr = dict(attrs)
        if tag == "meta":
            key = (attr.get("name") or attr.get("property") or "").lower()
            content = attr.get("content")
            if key in _DATE_META_KEYS and content:
                self.meta_dates.setdefault(key, []).append(_norm(content))
        elif tag == "time" and attr.get("datetime"):
            self.times.append(_norm(attr["datetime"]))
        elif tag in {"h1", "h2", "h3"} and self._heading_tag is None:
            self._heading_tag = tag
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == self._heading_tag:
            if tag == "h2":
                text = _norm(" ".join(self._buf))
                if text:
                    self.h2.append(text)
            self._heading_tag = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._heading_tag is not None:
            self._buf.append(data)


def _dates_in(values: list[str]) -> set[str]:
    found = set()
    for v in values:
        m = _DATE_RE.search(v)
        if m:
            found.add(m.group(0))
    return found


def page_truth(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hiveloom validator)"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
    p = _AuditParser()
    p.feed(raw)
    published = _dates_in(p.meta_dates.get("article:published_time", []))
    all_dates = _dates_in(
        [v for vals in p.meta_dates.values() for v in vals] + p.times
    )
    return {
        "h2": [_norm(h) for h in p.h2],
        "published": sorted(published),
        "dates": all_dates,
    }


def check(data: Any, expected_url: str, truth: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    stats: dict[str, Any] = {
        "count_true": len(truth["h2"]),
        "count_reported": None,
        "fabricated_h2": 0,
        "missing_h2": 0,
        "date_ok": None,
        "arith_ok": None,
    }
    if not isinstance(data, dict):
        return (["Output must be a single JSON object."], stats)

    expected = _norm(str(expected_url))
    if expected and data.get("source_url") != expected:
        problems.append(f"source_url must be exactly {expected!r}; got {data.get('source_url')!r}.")

    true_h2 = truth["h2"]
    true_set = set(true_h2)

    count = data.get("h2_count")
    stats["count_reported"] = count
    if not isinstance(count, int):
        problems.append("h2_count must be an integer.")
    elif count != len(true_h2):
        problems.append(
            f"h2_count is wrong: the page has {len(true_h2)} H2 headings, you reported {count}."
        )

    headings = data.get("h2_headings")
    if not isinstance(headings, list) or not all(isinstance(h, str) for h in headings):
        problems.append("h2_headings must be an array of strings.")
    else:
        normed = [_norm(h) for h in headings]
        fabricated = [h for h in normed if h not in true_set]
        missing = [h for h in true_h2 if h not in set(normed)]
        stats["fabricated_h2"] = len(fabricated)
        stats["missing_h2"] = len(missing)
        if isinstance(count, int) and len(headings) != count:
            problems.append(
                f"internally inconsistent: h2_count={count} but h2_headings has "
                f"{len(headings)} entries."
            )
        if len(fabricated) > 1:
            problems.append(
                f"These H2 headings are not on the live page: {fabricated[:5]!r}. "
                "Never invent headings you did not see."
            )
        if len(missing) > 1:
            problems.append(
                f"The page has H2 headings you did not list ({len(missing)} missing, "
                f"e.g. {missing[:3]!r}). h2_headings must be exhaustive."
            )

    pub = data.get("published_date")
    if pub is None:
        if truth["published"]:
            stats["date_ok"] = False
            problems.append(
                "published_date is null but the page declares "
                f"article:published_time {truth['published'][0]!r}."
            )
        else:
            stats["date_ok"] = True
    elif not isinstance(pub, str) or not _DATE_RE.fullmatch(pub):
        problems.append("published_date must be YYYY-MM-DD or null.")
    elif pub not in truth["dates"]:
        stats["date_ok"] = False
        problems.append(
            f"published_date {pub!r} does not appear in any date on the live page."
        )
    else:
        stats["date_ok"] = True

    days = data.get("days_to_2026")
    if isinstance(pub, str) and _DATE_RE.fullmatch(pub or ""):
        y, m, d = (int(x) for x in pub.split("-"))
        try:
            expected_days = (_REFERENCE - _dt.date(y, m, d)).days
        except ValueError:
            expected_days = None
            problems.append(f"published_date {pub!r} is not a real calendar date.")
        if expected_days is not None:
            if days != expected_days:
                stats["arith_ok"] = False
                problems.append(
                    f"days_to_2026 is wrong: from {pub} to 2026-01-01 is "
                    f"{expected_days} days, you reported {days!r}."
                )
            else:
                stats["arith_ok"] = True
    elif pub is None and days is not None:
        problems.append("days_to_2026 must be null when published_date is null.")

    return (problems, stats)


def validate(run_output: str, run_context: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(run_output)
    except (json.JSONDecodeError, TypeError):
        return {
            "passed": False,
            "feedback": "Output is not valid JSON. Emit a single raw JSON object.",
        }
    url = str(run_context.get("input", ""))
    try:
        truth = page_truth(url)
    except OSError as exc:
        return {"passed": False, "feedback": f"Could not re-fetch {url} to verify: {exc}"}
    problems, _stats = check(data, url, truth)
    return {"passed": not problems, "feedback": " ".join(problems)}
