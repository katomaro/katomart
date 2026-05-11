from __future__ import annotations

import html
import json
import re
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


SUPPORTED_HOST_PATTERNS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "cf-embed.play.hotmart.com",
    "pandavideo.com",
    "player.scaleup.com.br",
    "smartplayer.io",
    "play.gumlet.io",
    "safevideo.com",
    "spalla.io",
    "iframe.mediadelivery.net",
    "mediadelivery.net",
)

_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", re.IGNORECASE)
_GENERIC_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _matches_supported_host(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]
    for pattern in SUPPORTED_HOST_PATTERNS:
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def _matches_blacklist(url: str, blacklist: Iterable[str]) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    for entry in blacklist:
        entry = (entry or "").strip().lower()
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _normalize(url: str, base_url: str | None) -> str | None:
    if not url:
        return None
    url = html.unescape(url).strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    if url.startswith("/") and base_url:
        url = urljoin(base_url, url)
    if not url.lower().startswith(("http://", "https://")):
        return None
    return url


def _walk_json(node, sink: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower() in {"embedurl", "contenturl", "url", "src", "file", "streamurl", "streamurlhd", "streamurldefault", "hlsurl", "dashurl", "video", "player", "", "videourl"}:
                sink.append(value)
            else:
                _walk_json(value, sink)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, sink)


def extract_video_urls(
    html_text: str,
    base_url: str | None = None,
    blacklist: Iterable[str] | None = None,
) -> list[str]:
    """Return deduplicated, whitelisted video/player URLs found in ``html_text``.

    ``blacklist`` is a list of domains (matched as host or suffix) to drop even
    if they hit the supported-host whitelist. Pass
    ``settings.embed_domain_blacklist`` here.
    """
    blacklist = list(blacklist or [])
    candidates: list[str] = []

    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        soup = None

    if soup is not None:
        for tag in soup.find_all("iframe"):
            src = tag.get("src") or tag.get("data-src")
            if src:
                candidates.append(src)

        for tag in soup.find_all(["video", "source"]):
            src = tag.get("src") or tag.get("data-src")
            if src:
                candidates.append(src)
            for attr in ("data-hls", "data-mpd", "data-url"):
                val = tag.get(attr)
                if val:
                    candidates.append(val)

        for tag in soup.find_all("a", href=True):
            candidates.append(tag["href"])

        for script in soup.find_all("script"):
            script_type = (script.get("type") or "").lower()
            text = script.string or script.get_text() or ""
            if not text:
                continue
            if "ld+json" in script_type or "application/json" in script_type:
                try:
                    data = json.loads(text)
                    found: list[str] = []
                    _walk_json(data, found)
                    candidates.extend(found)
                except Exception:
                    pass
            candidates.extend(_M3U8_RE.findall(text))
            for match in _GENERIC_URL_RE.findall(text):
                if _matches_supported_host(match):
                    candidates.append(match)

        for tag in soup.find_all(attrs={"data-video-id": True}):
            for attr_name, attr_value in tag.attrs.items():
                if attr_name.startswith("data-") and isinstance(attr_value, str):
                    if attr_value.startswith(("http://", "https://", "//")):
                        candidates.append(attr_value)

    candidates.extend(_M3U8_RE.findall(html_text))

    seen: set[str] = set()
    result: list[str] = []
    for raw in candidates:
        url = _normalize(raw, base_url)
        if not url:
            continue
        if not _matches_supported_host(url):
            continue
        if _matches_blacklist(url, blacklist):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result
