from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

LOG = logging.getLogger("diya_ott_radar")
IST = ZoneInfo("Asia/Kolkata")
USER_AGENT = "Mozilla/5.0 (compatible; Diya-OTT-Radar/2.0; +https://github.com/purnavenkat777/diya-ott-radar)"
TELEGRAM_API = "https://api.telegram.org"
MAX_ARTICLE_BYTES = 600_000

# These searches discover reporting leads. They are not platform catalogue APIs.
DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=Telugu+movie+OTT+release+date+streaming+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+film+premiere+OTT+Netflix+Prime+JioHotstar+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+dubbed+movie+streaming+OTT+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+original+movie+OTT+streaming+release+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+movies+now+streaming+OTT+this+week+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
    "https://news.google.com/rss/search?q=Telugu+OTT+release+Netflix+OR+Prime+Video+OR+JioHotstar+OR+aha+OR+ZEE5+when%3A10d&hl=en-IN&gl=IN&ceid=IN%3Aen",
]

TELUGU_RE = re.compile(r"\b(telugu|tollywood|dubbed\s+in\s+telugu|telugu\s+dub(?:bed)?|telugu\s+audio)\b", re.I)
OTT_RE = re.compile(r"\b(ott|stream(?:ing)?|netflix|prime\s+video|amazon\s+prime|hotstar|jiohotstar|aha|zee5|sony\s*liv|sun\s*nxt|etv\s*win)\b", re.I)
MOVIE_RE = re.compile(r"\b(movie|movies|film|films|feature film|cinema|theatrical)\b", re.I)
WEB_SERIES_RE = re.compile(r"\b(web\s*series|series|season\s+\d+|episode)\b", re.I)
ROUNDUP_RE = re.compile(r"\b(this week|this weekend|weekly|week's|week of|ott releases|releases this week|watchlist|streaming guide|what to watch|movies to watch|new releases)\b", re.I)
RELEASE_RE = re.compile(r"\b(now streaming|streaming now|streaming on|streaming from|starts streaming|will stream|to stream|premiere[sd]?|digital premiere|ott debut|arrives? on ott|available on|releases? on|release date|streaming date|lands? on)\b", re.I)
DUB_RE = re.compile(r"\b(dubbed\s+in\s+telugu|telugu\s+dub(?:bed)?|telugu\s+version|dubbed\s+version\s+in\s+telugu)\b", re.I)
AUDIO_RE = re.compile(r"\b(telugu\s+audio|audio\s+(?:languages?|tracks?)\s*[:=-]?\s*(?:include\s*)?telugu|available\s+in\s+telugu\s+audio|telugu\s+language\s+audio)\b", re.I)
ORIGINAL_RE = re.compile(r"\b(original\s+telugu|telugu\s+(?:film|movie|cinema)|tollywood)\b", re.I)
NEGATIVE_RE = re.compile(r"\b(teaser|trailer|first look|motion poster|box office collection|review only|songs? launch)\b", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")

PLATFORM_PATTERNS = [
    ("Netflix", re.compile(r"\bnetflix\b", re.I)),
    ("Prime Video", re.compile(r"\b(prime\s*video|amazon\s*prime)\b", re.I)),
    ("JioHotstar", re.compile(r"\b(jiohotstar|hotstar)\b", re.I)),
    ("ZEE5", re.compile(r"\bzee\s*5\b", re.I)),
    ("aha", re.compile(r"\baha\b", re.I)),
    ("Sony LIV", re.compile(r"\bsony\s*liv\b", re.I)),
    ("Sun NXT", re.compile(r"\bsun\s*nxt\b", re.I)),
    ("ETV Win", re.compile(r"\betv\s*win\b", re.I)),
]
MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
RELEASE_DATE_RE = re.compile(
    rf"\b(?:stream(?:s|ing)?|premiere[sd]?|release(?:s|d)?|available)\b[^.\n]{{0,35}}?\b({MONTH}\s+\d{{1,2}}(?:,\s*\d{{4}})?|\d{{1,2}}\s+{MONTH}(?:\s+\d{{4}})?)\b",
    re.I,
)


@dataclass(frozen=True)
class Candidate:
    headline: str
    summary: str
    feed_url: str
    article_url: str
    publisher: str
    published: datetime | None
    meta_title: str = ""
    meta_description: str = ""
    headings: tuple[str, ...] = ()

    @property
    def searchable_text(self) -> str:
        return " ".join((self.headline, self.summary, self.meta_title, self.meta_description, " ".join(self.headings)))


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
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        LOG.warning("Invalid RSS/Atom XML: %s", exc)
        return []

    entries: list[dict[str, str]] = []
    items = root.findall(".//item")
    if items:
        for item in items:
            entries.append({
                "title": item.findtext("title", default=""),
                "link": item.findtext("link", default=""),
                "summary": item.findtext("description", default=""),
                "published": item.findtext("pubDate", default=""),
            })
        return entries

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


def fetch_feed(feed_url: str, days_back: int = 10) -> list[Candidate]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    request = urllib.request.Request(feed_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        entries = _feed_entries(response.read())

    results: list[Candidate] = []
    for entry in entries:
        headline = clean_text(entry.get("title", ""))
        url = (entry.get("link", "") or "").strip()
        if not headline or not url:
            continue
        published = parse_published(entry.get("published", ""))
        if published and published < cutoff:
            continue
        publisher = urlparse(url).netloc.removeprefix("www.") or "Unknown publisher"
        results.append(Candidate(
            headline=headline,
            summary=clean_text(entry.get("summary", "")),
            feed_url=feed_url,
            article_url=url,
            publisher=publisher,
            published=published,
        ))
    return results


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.headings: list[str] = []
        self._heading_depth = 0
        self._heading_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "meta":
            values = {k.lower(): (v or "") for k, v in attrs}
            key = values.get("property") or values.get("name")
            content = values.get("content", "")
            if key and content and key.lower() in ("og:title", "og:description", "twitter:title", "twitter:description", "description"):
                self.meta.setdefault(key.lower(), clean_text(content))
        elif tag.lower() == "link":
            values = {k.lower(): (v or "") for k, v in attrs}
            if "canonical" in values.get("rel", "").lower().split() and values.get("href"):
                self.canonical = values["href"].strip()
        elif tag.lower() in ("h2", "h3"):
            self._heading_depth += 1
            self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if self._heading_depth:
            self._heading_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("h2", "h3") and self._heading_depth:
            heading = clean_text(" ".join(self._heading_buffer))
            if heading and heading not in self.headings and len(heading) <= 180:
                self.headings.append(heading)
            self._heading_depth = 0
            self._heading_buffer = []


def enrich_candidate(item: Candidate) -> Candidate:
    """Resolve Google News redirects and read basic page metadata; never trust metadata as audio proof."""
    request = urllib.request.Request(item.article_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            final_url = response.geturl()
            raw = response.read(MAX_ARTICLE_BYTES)
        parser = MetadataParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        canonical = urllib.parse.urljoin(final_url, parser.canonical) if parser.canonical else final_url
        # Keep a publisher URL only when it remains HTTP(S); ignore malformed canonical tags.
        parsed = urlparse(canonical)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            canonical = final_url
        publisher = urlparse(canonical).netloc.removeprefix("www.") or item.publisher
        meta_title = parser.meta.get("og:title") or parser.meta.get("twitter:title") or ""
        meta_description = (parser.meta.get("og:description") or parser.meta.get("twitter:description") or parser.meta.get("description") or "")[:500]
        return replace(item, article_url=canonical, publisher=publisher, meta_title=meta_title, meta_description=meta_description, headings=tuple(parser.headings[:40]))
    except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as exc:
        LOG.info("Could not enrich article link %s: %s", item.article_url, exc)
        return item


def discover(feeds: Iterable[str] | None = None, days_back: int = 10, max_candidates: int = 120) -> list[Candidate]:
    raw: list[Candidate] = []
    for feed in feeds or get_feed_urls():
        try:
            raw.extend(fetch_feed(feed, days_back=days_back))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            LOG.warning("Feed unavailable %s: %s", feed, exc)
    raw = raw[:max_candidates]

    # Enrich only likely Telugu/OTT candidates to avoid unnecessary publisher requests.
    prelim = [item for item in raw if is_relevant(item)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        enriched = list(pool.map(enrich_candidate, prelim))
    return deduplicate(enriched)


def is_relevant(item: Candidate) -> bool:
    text = item.searchable_text
    if not (TELUGU_RE.search(text) and OTT_RE.search(text)):
        return False
    if NEGATIVE_RE.search(item.headline) and not RELEASE_RE.search(text):
        return False
    if WEB_SERIES_RE.search(item.headline) and not MOVIE_RE.search(item.headline):
        return False
    # A Telugu mention plus a platform name is not enough: require a release/streaming context.
    if not (RELEASE_RE.search(text) or ROUNDUP_RE.search(item.headline)):
        return False
    return True


def is_roundup(item: Candidate) -> bool:
    return bool(ROUNDUP_RE.search(item.headline))


def platform_names(item: Candidate) -> list[str]:
    text = item.searchable_text
    return [name for name, pattern in PLATFORM_PATTERNS if pattern.search(text)]


def telugu_evidence(item: Candidate) -> str:
    text = item.searchable_text
    if AUDIO_RE.search(text):
        return "Source explicitly mentions Telugu audio; platform track not independently verified"
    if DUB_RE.search(text):
        return "Telugu-dub claim in source; platform track not independently verified"
    if ORIGINAL_RE.search(text):
        return "Original Telugu film mentioned; platform audio still needs checking"
    return "Telugu is mentioned; specific audio track is not confirmed"


def extract_release_date(item: Candidate) -> str | None:
    match = RELEASE_DATE_RE.search(item.searchable_text)
    return match.group(1) if match else None


def _normalise_story_key(item: Candidate) -> str:
    text = item.headline.lower()
    text = re.sub(r"\s+[|–—-]\s+[^|–—-]+$", "", text)
    text = re.sub(r"\b(ott|streaming|release date|this week|this weekend|latest|new|telugu|movies?|films?)\b", " ", text)
    text = re.sub(r"\b(?:nov|dec|jan|feb|mar|apr|may|jun|jul|aug|sep|oct)\w*\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def deduplicate(items: list[Candidate]) -> list[Candidate]:
    seen_urls: set[str] = set()
    best_by_key: dict[str, Candidate] = {}
    for item in items:
        url = item.article_url.split("?")[0].rstrip("/").lower()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        key = _normalise_story_key(item) or item.headline.casefold()
        current = best_by_key.get(key)
        # Prefer detailed publisher pages and non-roundup stories over generic summaries.
        rank = (bool(item.meta_description), not is_roundup(item), bool(platform_names(item)), bool(item.published))
        current_rank = (bool(current.meta_description), not is_roundup(current), bool(platform_names(current)), bool(current.published)) if current else None
        if current is None or rank > current_rank:
            best_by_key[key] = item
    # Stable chronological ordering, newest first; undated stories last.
    return sorted(best_by_key.values(), key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def display_story_title(item: Candidate) -> str:
    # Metadata titles often contain publisher suffixes; retain headline because it is the searched source headline.
    title = item.headline
    title = re.sub(r"\s+[|–—-]\s+[^|–—-]+$", "", title).strip()
    return title[:220]


def format_article_date(value: datetime | None) -> str:
    return value.astimezone(IST).strftime("%d %b %Y") if value else "Not supplied"


def roundup_titles(item: Candidate) -> list[str]:
    """Extract only headings that explicitly look like a film/OTT release item."""
    results: list[str] = []
    seen: set[str] = set()
    for heading in item.headings:
        value = re.sub(r"^\s*(?:\d+|[•*-])\s*[.)-]?\s*", "", heading).strip()
        if len(value) < 3 or len(value) > 110:
            continue
        if re.search(r"\b(also read|related articles|trending|read more|watch now|subscribe|advertisement)\b", value, re.I):
            continue
        # We only extract a heading as a film lead when it carries an OTT/release signal.
        if not re.search(r"\b(ott|stream(?:ing)?|premiere|release|released|dubbed|telugu)\b", value, re.I):
            continue
        value = re.sub(r"\s*(?:[-–—:]\s*)?(?:ott|digital)?\s*release\s*(?:date)?.*$", "", value, flags=re.I).strip(" -–—:")
        value = re.sub(r"\s+(?:starts? streaming|now streaming|streaming on|to stream|streams? on|premieres? on|available on)\b.*$", "", value, flags=re.I).strip(" -–—:")
        if len(value) < 3:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        if key and key not in seen:
            seen.add(key)
            results.append(value)
    return results[:12]


def build_report(items: list[Candidate], now: datetime | None = None, max_items: int = 15) -> str:
    now = now or datetime.now(IST)
    relevant = [item for item in items if is_relevant(item)]
    specific = [item for item in relevant if not is_roundup(item)]
    roundups = [item for item in relevant if is_roundup(item)]

    lines = [
        "🎬 DIYA OTT RADAR · V2",
        f"Weekly Telugu OTT discovery · {now.astimezone(IST):%A, %d %B %Y}",
        "Original Telugu + Telugu-dubbed movie leads",
        "",
        f"Film-specific stories: {min(len(specific), max_items)} · Roundup references: {min(len(roundups), 4)}",
        "",
    ]

    if specific:
        lines.append("🎞 FILM-SPECIFIC STORIES")
        for index, item in enumerate(specific[:max_items], 1):
            platforms = ", ".join(platform_names(item)) or "Platform not identified in source"
            release = extract_release_date(item) or "Not clearly stated in source"
            lines.extend([
                f"{index}. {display_story_title(item)}",
                f"📺 Platform: {platforms}",
                f"🗓 OTT date: {release}",
                f"🎧 Telugu check: {telugu_evidence(item)}",
                f"📰 Article: {item.publisher} · {format_article_date(item.published)}",
                f"🔗 {item.article_url}",
                "",
            ])
    else:
        lines.extend(["🎞 FILM-SPECIFIC STORIES", "No strong single-film OTT announcement was found in the monitored feeds.", ""])

    if roundups:
        lines.append("🗂 WEEKLY ROUNDUP REFERENCES")
        for item in roundups[:4]:
            mentioned = roundup_titles(item)
            lines.extend([
                f"• {display_story_title(item)}",
                f"  {item.publisher} · {format_article_date(item.published)}",
            ])
            if mentioned:
                lines.append("  Titles surfaced in article headings (verify individually):")
                lines.extend(f"  - {title}" for title in mentioned[:8])
            else:
                lines.append("  Individual movie names could not be extracted reliably from the page.")
            lines.append(f"  {item.article_url}")
        lines.append("")

    lines.extend([
        "⚠️ VERIFICATION NOTE",
        "These are publisher-discovery leads, not a verified OTT catalogue. An article mention does not prove the movie is streaming today or that Telugu audio is available. Check the title page's Audio / Languages section in the OTT app before watching.",
        "Platforms monitored in searches: Netflix · Prime Video · JioHotstar · ZEE5 · aha · Sony LIV · Sun NXT · ETV Win",
        "",
        "— Diya OTT Radar",
    ])
    return "\n".join(lines)


def telegram_send(token: str, chat_id: str, message: str) -> None:
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub Actions secret.")
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
        data = json.dumps({"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}).encode("utf-8")
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
            raise RuntimeError(f"Telegram rejected message: {payload.get('description', 'unknown error')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Telugu OTT movie release leads and send a Telegram report.")
    parser.add_argument("--test", action="store_true", help="Send a Telegram connection test without searching feeds.")
    parser.add_argument("--preview", action="store_true", help="Print a report without sending to Telegram.")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if args.test:
        telegram_send(token, chat_id, "✅ Diya OTT Radar V2: Telegram connection successful. Friday reports target 8:00 AM IST.")
        print("Telegram test sent.")
        return 0

    items = discover(days_back=int(os.getenv("DAYS_BACK", "10")))
    report = build_report(items, max_items=int(os.getenv("MAX_ITEMS", "15")))
    print(report)
    if not args.preview:
        telegram_send(token, chat_id, report)
    LOG.info("Processed %d deduplicated Telugu/OTT leads.", len(items))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOG.exception("Diya OTT Radar failed: %s", exc)
        raise SystemExit(1)
