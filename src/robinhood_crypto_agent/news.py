"""News feeds (RSS/Atom) as NewsItems, and the store they land in.

Everything here is untrusted text from the internet. It is stored, labeled by
Jev, and shown to System 2 as evidence to weigh. It is never executed or
followed, and it never reaches an order field.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import net
from .errors import AgentError
from .models import NewsItem, NewsLabels, parse_timestamp, utcnow

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

SUMMARY_CHARS = 400
TITLE_CHARS = 300


def item_id(source: str, key: str) -> str:
    return hashlib.sha256(f"{source}|{key}".encode()).hexdigest()[:16]


def _clean(text: str | None, limit: int) -> str:
    """Strip tags and entities from feed HTML and collapse whitespace."""
    if not text:
        return ""
    plain = html.unescape(_TAG.sub(" ", text))
    return _SPACE.sub(" ", plain).strip()[:limit]


def _child_text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _parse_date(text: str | None) -> datetime | None:
    """ISO 8601 (Atom) or RFC 822 (RSS); ``None`` rather than a guess."""
    if not text:
        return None
    try:
        return parse_timestamp(text)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def feed_source(url: str) -> str:
    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")


def parse_feed(raw: bytes, *, source: str) -> list[NewsItem]:
    """Parse an RSS 2.0 or Atom document.

    An entry without a parseable date is dropped: there is no way to place it
    in the news window, and stamping it "now" would make old news look fresh.

    A document that declares XML entities is refused outright. No feed needs
    them, and entity expansion is how a kilobyte of XML becomes gigabytes in
    memory; refusing them does not depend on how new the local expat is.
    """
    if b"<!ENTITY" in raw:
        raise AgentError(f"{source}: the feed declares XML entities; refusing to parse it")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AgentError(f"{source}: not a valid RSS/Atom document ({exc})") from exc

    items: list[NewsItem] = []
    for node in root.iter("item"):
        title = _clean(_child_text(node, "title"), TITLE_CHARS)
        link = _child_text(node, "link")
        key = _child_text(node, "guid") or link or title
        published = _parse_date(_child_text(node, "pubDate"))
        if title and key and published:
            items.append(
                NewsItem(
                    item_id=item_id(source, key),
                    source=source,
                    title=title,
                    published_at=published,
                    url=link,
                    summary=_clean(_child_text(node, "description"), SUMMARY_CHARS),
                )
            )

    for node in root.iter(f"{_ATOM}entry"):
        title = _clean(_child_text(node, f"{_ATOM}title"), TITLE_CHARS)
        link_node = node.find(f"{_ATOM}link")
        link = link_node.get("href") if link_node is not None else None
        key = _child_text(node, f"{_ATOM}id") or link or title
        published = _parse_date(
            _child_text(node, f"{_ATOM}published") or _child_text(node, f"{_ATOM}updated")
        )
        summary = _child_text(node, f"{_ATOM}summary") or _child_text(node, f"{_ATOM}content")
        if title and key and published:
            items.append(
                NewsItem(
                    item_id=item_id(source, key),
                    source=source,
                    title=title,
                    published_at=published,
                    url=link,
                    summary=_clean(summary, SUMMARY_CHARS),
                )
            )
    return items


def fetch_feed(url: str, *, http: Callable[..., bytes] = net.request) -> list[NewsItem]:
    source = feed_source(url)
    return parse_feed(http("GET", url, timeout=15), source=source)


# -- storage ------------------------------------------------------------------


def _item_from_row(row: dict[str, Any]) -> NewsItem | None:
    try:
        labels_raw = row.get("labels")
        labels = (
            NewsLabels(
                asset=str(labels_raw["asset"]),
                asset_confidence=float(labels_raw["asset_confidence"]),
                direction=str(labels_raw["direction"]),
                direction_confidence=float(labels_raw["direction_confidence"]),
                impact=float(labels_raw["impact"]),
                model=str(labels_raw.get("model", "")),
            )
            if isinstance(labels_raw, dict)
            else None
        )
        return NewsItem(
            item_id=str(row["item_id"]),
            source=str(row["source"]),
            title=str(row["title"]),
            published_at=parse_timestamp(str(row["published_at"])),
            url=row.get("url"),
            summary=str(row.get("summary") or ""),
            labels=labels,
        )
    except (KeyError, TypeError, ValueError):
        return None


class NewsStore:
    """Append-only JSONL of fetched items, like the price store.

    Each row also records ``fetched_at``, when the agent first saw the item.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, items: Iterable[NewsItem]) -> int:
        fetched_at = utcnow().isoformat()
        rows = [{**item.to_dict(), "fetched_at": fetched_at} for item in items]
        if not rows:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        return len(rows)

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn line costs one headline, not the store
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def items(self) -> list[NewsItem]:
        return [item for row in self._rows() if (item := _item_from_row(row)) is not None]

    def recent(self, since: datetime) -> list[NewsItem]:
        """Items published at or after ``since``, newest first."""
        found = [item for item in self.items() if item.published_at >= since]
        return sorted(found, key=lambda i: i.published_at, reverse=True)

    def seen_ids(self) -> set[str]:
        return {str(row.get("item_id")) for row in self._rows()}


def with_labels(item: NewsItem, labels: NewsLabels | None) -> NewsItem:
    return replace(item, labels=labels)
