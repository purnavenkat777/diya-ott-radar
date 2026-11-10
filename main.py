from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sys
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


LOG = logging.getLogger("diya_ott_radar")
IST = ZoneInfo("Asia/Kolkata")
USER_AGENT = "Diya-OTT-Radar/1.0 (+https://github.com/purnavenkat777/diya-ott-radar)"
TELEGRAM_API = "https://api.telegram.org"

# News-search feeds are discovery sources, not official OTT catalogues.
DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=Telugu+OTT+releases+this+week+India+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+movies+streaming+this+Friday+OTT+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+dubbed+OTT+release+Netflix+Prime+Hotstar+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+OTT+release+date+aha+ZEE5+SunNXT+ETV+Win+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
]

TELUGU_RE = re.compile(
    r"\b(telugu|tollywood|dubbed\s+in\s+telugu|telugu\s+dub(?:bed)?|telugu\s+audio)\b",
    re.IGNORECASE,
)
OTT_RE = re.compile(
    r"\b(ott|stream(?:ing)?|netflix|prime\s+video|amazon\s+prime|hotstar|jiohotstar|aha|zee5|sony\s*liv|sun\s*nxt|etv\s*win)\b",
    re.IGNORECASE,
)
DUB_RE = re.compile(r"\b(dubbed|dub\s+version|dubbed\s+in\s+telugu|telugu\s+dub)\b", re.I)
ORIGINAL_RE = re.compile(r"\b(telugu\s+(?:film|movie)|tollywood|originally\s+in\s+telugu)\b", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Candidate:
    title: str
    summary: str
    url: str
    source: str
    published: datetime | None


def clean_text(value: str) -> str:
    value = HTML_TAG_RE.sub(" ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def get_feed_urls() -> list[str]:
    raw = os.getenv("OTT_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    return [url.strip() for url in raw.split(",") if url.strip()]


def _feed_entries(xml_bytes: bytes) -> list[dict[str, str]]:
    """Parse common RSS 2.0 and Atom feeds using only the Python standard library."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        LOG.warning("Invalid RSS/Atom XML: %s", exc)
        return []

    entries: list[dict[str, str]] = []
    rss_items = root.findall(".//item")
    if rss_items:
        for item in rss_items:
            entries.append({
                "title": item.findtext("title", default=""),
                "link": item.findtext("link", default=""),
                "summary": item.findtext("description", default=""),
                "published": item.findtext("pubDate", default=""),
            })
        return entries

    # Atom uses namespaces that vary by version, so inspect local tag names.
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "entry":
            continue
        record = {"title": "", "link": "", "summary": "", "published": ""}
        for child in list(node):
            name = child.tag.rsplit("}", 1)[-1]
            if name == "title":
                record["title"] = child.text or ""
            elif name in ("summary", "content") and not record["summary"]:
                record["summary"] = "".join(child.itertext())
            elif name == "link" and not record["link"]:
                href = child.attrib.get("href", "")
                if child.attrib.get("rel", "alternate") == "alternate":
                    record["link"] = href
            elif name in ("published", "updated") and not record["published"]:
                record["published"] = child.text or ""
        entries.append(record)
    return entries


def discover(feeds: Iterable[str], days_back: int = 10, limit: int = 100) -> list[Candidate]:
    """Collect recent article leads. It deliberately does not assert OTT availability."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days_back)
    found: list[Candidate] = []
    seen: set[str] = set()
    headers = {"User-Agent": USER_AGENT}

    for feed_url in feeds:
        try:
            request = urllib.request.Request(feed_url, headers=headers)
            with urllib.request.urlopen(request, timeout=25) as response:
                xml_bytes = response.read()
            entries = _feed_entries(xml_bytes)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            LOG.warning("Could not fetch feed %s: %s", feed_url, exc)
            continue

        for entry in entries:
            title = clean_text(entry.get("title", ""))
            url = (entry.get("link", "") or "").strip()
            summary = clean_text(entry.get("summary", ""))
            if not title or not url:
                continue

            published = parse_published(entry.get("published", ""))
            if published and published < cutoff:
                continue

            key = url.split("?")[0].rstrip("/").lower() or title.casefold()
            if key in seen:
                continue
            seen.add(key)
            source = urlparse(url).netloc.removeprefix("www.") or "Unknown source"
            found.append(Candidate(title, summary, url, source, published))
            if len(found) >= limit:
                return found
    return found


def is_relevant(item: Candidate) -> bool:
    text = f"{item.title} {item.summary}"
    return bool(TELUGU_RE.search(text) and OTT_RE.search(text))


def classify_telugu(item: Candidate) -> str:
    """Classify article wording; this is not a verified platform audio-track check."""
    text = f"{item.title} {item.summary}"
    if DUB_RE.search(text):
        return "Dubbed into Telugu (article mention; platform audio unverified)"
    if ORIGINAL_RE.search(text):
        return "Original Telugu (article mention; platform audio unverified)"
    return "Telugu mentioned (audio track unverified)"


def format_date(value: datetime | None) -> str:
    return value.astimezone(IST).strftime("%d %b %Y") if value else "Not stated by source"


def build_report(items: list[Candidate], now: datetime | None = None, max_items: int = 20) -> str:
    now = now or datetime.now(IST)
    selected = [item for item in items if is_relevant(item)][:max_items]
    lines = [
        "🎬 DIYA OTT RADAR",
        f"Telugu OTT discovery · {now.astimezone(IST):%A, %d %B %Y}",
        "",
        "Original Telugu + Telugu-dubbed movie leads",
        "",
    ]

    if not selected:
        lines.append("No matching Telugu OTT news leads were found in the monitored feeds this week.")
    else:
        for index, item in enumerate(selected, start=1):
            lines.extend([
                f"{index}. {item.title}",
                f"🎧 Language: {classify_telugu(item)}",
                f"🗓 Article date: {format_date(item.published)}",
                f"📰 Source: {item.source}",
                f"🔗 {item.url}",
                "",
            ])

    lines.extend([
        "⚠️ Verification: These are news-discovery leads, not confirmed release listings. An article mentioning Telugu does not prove a Telugu audio track is available. Open the OTT title page and check Audio / Languages before watching.",
        "Platforms to check: Netflix · Prime Video · JioHotstar · ZEE5 · aha · Sony LIV · Sun NXT · ETV Win",
        "",
        "— Diya OTT Radar",
    ])
    return "\n".join(lines)


def telegram_send(token: str, chat_id: str, message: str) -> None:
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub Actions secret.")

    # Telegram message text limit is 4096 characters. Split on line boundaries.
    chunks: list[str] = []
    current = ""
    for line in message.splitlines():
        proposed = f"{current}\n{line}" if current else line
        if len(proposed) > 3900 and current:
            chunks.append(current)
            current = line
        else:
            current = proposed
    if current:
        chunks.append(current)

    for chunk in chunks:
        data = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Telegram: {exc}") from exc
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram rejected the message: {payload.get('description', 'unknown error')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Telugu OTT news leads and send a Telegram report.")
    parser.add_argument("--test", action="store_true", help="Send a Telegram connection test without searching feeds.")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if args.test:
        telegram_send(token, chat_id, "✅ Diya OTT Radar: Telegram connection successful. Friday reports are configured for 8:00 AM IST.")
        print("Telegram test sent.")
        return 0

    max_items = int(os.getenv("MAX_ITEMS", "20"))
    candidates = discover(get_feed_urls())
    report = build_report(candidates, max_items=max_items)
    print(report)
    telegram_send(token, chat_id, report)
    LOG.info("Sent report using %d discovery candidates.", len(candidates))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOG.exception("Diya OTT Radar failed: %s", exc)
        raise SystemExit(1)
