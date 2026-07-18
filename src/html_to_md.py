"""Email body conversion: HTML to Markdown, plus quoted-history trimming.

Emails are converted to Markdown to save tokens. By default the quoted
history (previous messages in the thread) is cut off; callers can request
the full body instead. Trimming is heuristic: it recognizes the separators
produced by Outlook, OWA, Gmail, Yahoo and Apple Mail, and a few common
plain-text markers.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

# Plain-text (and post-conversion Markdown) lines that start a quoted block.
_QUOTE_LINE_PATTERNS = [
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*Исходное сообщение\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{7,}\s*$"),  # Outlook plain-text divider
    re.compile(r"^\s*On .{4,200} wrote:\s*$"),
    re.compile(r"^\s*\d{1,2}[./]\d{1,2}[./]\d{2,4}.{0,200}(?:пишет|wrote):\s*$"),
]

# "From:" header block that Outlook inserts above the quoted message. Only
# treated as a quote marker when followed shortly by a "Sent:"/"Date:" line,
# to avoid false positives on ordinary body text.
_FROM_RE = re.compile(r"^\s*\**\s*(From|От):\s*\**\s*\S", re.I)
_SENT_RE = re.compile(r"^\s*\**\s*(Sent|Date|Отправлено|Дата):\s*\**", re.I)


def _cut_from(el) -> None:
    """Remove *el* and everything after it in document order."""
    node = el
    while node is not None and node.name != "[document]":
        parent = node.parent
        sibling = node.next_sibling
        while sibling is not None:
            nxt = sibling.next_sibling
            sibling.extract()
            sibling = nxt
        node = parent
    el.decompose()


def _is_outlook_separator(tag) -> bool:
    # Outlook desktop marks the quoted part with a header block like
    # <div style="border:none;border-top:solid #E1E1E1 1.0pt;...">.
    if tag.name != "div":
        return False
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "border:none" in style and "border-top:solid" in style


def _strip_quoted_html(soup: BeautifulSoup) -> bool:
    """Remove quoted history from parsed HTML. Returns True if anything was cut."""
    trimmed = False

    # OWA: <hr> + <div id="divRplyFwdMsg"> header, or <div id="appendonsend">.
    for marker_id in ("divRplyFwdMsg", "appendonsend"):
        el = soup.find(attrs={"id": marker_id})
        if el is not None:
            hr = el.find_previous_sibling("hr")
            _cut_from(el)
            if hr is not None:
                hr.decompose()
            trimmed = True

    el = soup.find(_is_outlook_separator)
    if el is not None:
        _cut_from(el)
        trimmed = True

    for name, attrs in (
        ("blockquote", {}),
        ("div", {"class": "gmail_quote"}),
        ("div", {"class": "yahoo_quoted"}),
    ):
        for el in soup.find_all(name, attrs=attrs):
            el.decompose()
            trimmed = True

    return trimmed


def _strip_quoted_text(text: str) -> tuple[str, bool]:
    """Cut *text* at the first line that looks like a quote separator."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if any(p.match(line) for p in _QUOTE_LINE_PATTERNS):
            return "\n".join(lines[:i]), True
        if _FROM_RE.match(line):
            lookahead = lines[i + 1 : i + 5]
            if any(_SENT_RE.match(nxt) for nxt in lookahead):
                return "\n".join(lines[:i]), True
    return text, False


def _normalize(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def body_to_markdown(body: str, is_html: bool, include_quoted: bool) -> tuple[str, bool]:
    """Convert an email body to Markdown.

    Returns (markdown, trimmed) where *trimmed* tells whether quoted history
    was cut off. When *include_quoted* is True nothing is trimmed.
    """
    if not body:
        return "", False

    trimmed = False
    if is_html:
        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["script", "style", "head", "meta", "title"]):
            tag.decompose()
        if not include_quoted:
            trimmed = _strip_quoted_html(soup)
        text = markdownify(str(soup), heading_style="ATX", strip=["img"])
    else:
        text = body

    if not include_quoted:
        text, cut = _strip_quoted_text(text)
        trimmed = trimmed or cut

    return _normalize(text), trimmed


def make_snippet(text: str | None, length: int = 200) -> str:
    """First *length* characters of *text* with whitespace collapsed."""
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= length:
        return collapsed
    return collapsed[:length].rstrip() + "…"
