"""Deterministic fetch-and-distill tool for the article-extractor harness.

The context manager clips every tool result to its first 8,000 characters
(``TOOL_RESULT_MAX_CHARS``), so raw HTML loses its deep-body content before
the model ever sees it. This tool does the deterministic part in code: fetch
the page, parse it with the stdlib HTML parser, and return a labeled digest —
title, relevant metadata, every h1/h2/h3 in document order, and the lead
visible text — that always fits inside the clip. The model's only job is to
map digest lines onto the output schema.
"""

from __future__ import annotations

import re
import urllib.request
from html.parser import HTMLParser

from hiveloom.tools import tool

_META_KEYS = {
    "description",
    "author",
    "date",
    "og:title",
    "og:description",
    "og:type",
    "article:author",
    "article:published_time",
    "article:modified_time",
    "twitter:title",
    "twitter:description",
    "parsely-pub-date",
    "publish-date",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "nav", "footer"}
_HEADING_TAGS = {"h1", "h2", "h3"}
_MAX_HEADINGS = 30
_MAX_LEAD_CHARS = 2000
_MAX_TAIL_CHARS = 1500
_MAX_BODY_CHARS = 100_000
_MAX_DIGEST_CHARS = 7500


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.metas: list[tuple[str, str]] = []
        self.times: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.body_text: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []
        self._body_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attr.get("name") or attr.get("property") or "").lower()
            content = attr.get("content")
            if key in _META_KEYS and content:
                self.metas.append((key, _norm(content)))
        elif tag == "time" and attr.get("datetime"):
            self.times.append(_norm(attr["datetime"]))
        elif tag in _HEADING_TAGS and self._heading_tag is None:
            self._heading_tag = tag
            self._heading_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == self._heading_tag:
            text = _norm(" ".join(self._heading_buf))
            if text:
                self.headings.append((tag, text))
            self._heading_tag = None
            self._heading_buf = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._heading_tag is not None:
            self._heading_buf.append(data)
        elif self._body_chars < _MAX_BODY_CHARS:
            text = _norm(data)
            if text:
                self.body_text.append(text)
                self._body_chars += len(text) + 1


def _digest(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (hiveloom article-extractor)"}
    )
    try:
        raw = urllib.request.urlopen(req, timeout=20).read()
    except OSError as exc:
        return f"ERROR: could not fetch {url}: {exc}"
    html_text = raw.decode("utf-8", errors="replace")

    parser = _PageParser()
    parser.feed(html_text)

    lines = [f"FETCHED: {url}"]
    title = _norm(" ".join(parser.title_parts))
    if title:
        lines.append(f"TITLE: {title}")
    for key, content in parser.metas:
        lines.append(f"META {key}: {content}")
    for value in parser.times[:5]:
        lines.append(f"TIME: {value}")
    for tag, text in parser.headings[:_MAX_HEADINGS]:
        lines.append(f"{tag.upper()}: {text}")
    body = _norm(" ".join(parser.body_text))
    if body:
        lines.append(f"LEAD TEXT: {body[:_MAX_LEAD_CHARS]}")
    if len(body) > _MAX_LEAD_CHARS:
        lines.append(f"TAIL TEXT: {body[-_MAX_TAIL_CHARS:]}")
    return "\n".join(lines)[:_MAX_DIGEST_CHARS]


@tool(
    description=(
        "HTTP GET a web page and return a compact deterministic digest: "
        "TITLE / META / TIME / H1 / H2 / H3 / LEAD TEXT lines, always under 8KB. "
        "On failure returns a line starting with 'ERROR:'."
    ),
    tags=["network", "read"],
)
def fetch_clean(url: str) -> str:
    """Fetch a page and distill it into labeled digest lines for extraction."""
    return _digest(url)
