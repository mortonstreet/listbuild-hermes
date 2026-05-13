from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


BLOCKED_DISCOVERY_HOSTS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "wikipedia.org",
    "crunchbase.com",
    "pitchbook.com",
    "glassdoor.com",
    "indeed.com",
    "bloomberg.com",
    "google.com",
    "googleusercontent.com",
    "maps.google.com",
    "news.google.com",
}


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    original_url: str
    normalized_url: str
    host: str
    homepage_url: str


def _strip_www(host: str) -> str:
    host = host.lower().strip(".")
    if host.startswith("www."):
        return host[4:]
    return host


def normalize_url(url: str) -> NormalizedUrl | None:
    raw = url.strip()
    if not raw:
        return None

    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None

    host = _strip_www(parsed.netloc)
    normalized_url = parsed._replace(netloc=host, fragment="").geturl()
    homepage_url = f"{parsed.scheme}://{host}/"
    return NormalizedUrl(
        original_url=url,
        normalized_url=normalized_url,
        host=host,
        homepage_url=homepage_url,
    )


def is_discovery_host_allowed(host: str) -> bool:
    lowered = host.lower()
    if lowered in BLOCKED_DISCOVERY_HOSTS:
        return False
    return not any(
        lowered == blocked or lowered.endswith(f".{blocked}")
        for blocked in BLOCKED_DISCOVERY_HOSTS
    )
