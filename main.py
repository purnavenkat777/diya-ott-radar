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
