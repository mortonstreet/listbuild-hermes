from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urljoin, urlparse


PAGE_KEYWORDS = (
    "about",
    "services",
    "solutions",
    "industries",
    "customers",
    "clients",
    "pricing",
    "news",
    "press",
    "blog",
    "portfolio",
    "investment",
    "investments",
    "case-study",
    "case-studies",
    "careers",
    "jobs",
    "team",
)

COMMON_RESEARCH_PATHS = (
    "",
    "about",
    "services",
    "solutions",
    "industries",
    "customers",
    "clients",
    "pricing",
    "news",
    "press",
    "blog",
    "portfolio",
    "investments",
    "case-studies",
    "careers",
    "jobs",
    "team",
)

BRIGHTDATA_DISCOVERY_PATHS = (
    "",
    "robots.txt",
    "sitemap.xml",
    "sitemap_index.xml",
    "sitemap-index.xml",
    "wp-sitemap.xml",
    "sitemap.txt",
)

NEGATIVE_PAGE_KEYWORDS = (
    "privacy",
    "terms",
    "cookie",
    "login",
    "signin",
    "sign-in",
    "portal",
    "account",
    "cart",
    "checkout",
    "tag",
    "category",
    "author",
    "feed",
    "search",
    "events",
    "webinar",
)

EXCLUDED_URL_SCHEMES = ("mailto:", "tel:", "javascript:", "#")
EXCLUDED_URL_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".pdf",
    ".zip",
    ".xml",
    ".gz",
    ".mp4",
    ".mov",
    ".mp3",
)


@dataclass(frozen=True, slots=True)
class ResearchPage:
    url: str
    title: str
    description: str
    markdown: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class CompanyResearchSummary:
    description: str
    offer: str
    icp: str
    painpoint: str
    signals: str
    signal_sources: str


def summarize_company_posts(
    company_posts_payload: dict[str, Any] | None,
    *,
    max_posts: int = 3,
    snippet_chars: int = 220,
) -> tuple[str, str]:
    if not isinstance(company_posts_payload, dict):
        return "", ""
    elements = company_posts_payload.get("elements")
    if not isinstance(elements, list):
        return "", ""

    snippets: list[str] = []
    urls: list[str] = []
    for item in elements[:max_posts]:
        if not isinstance(item, dict):
            continue
        content = _collapse_whitespace(str(item.get("content") or ""))
        if content:
            snippets.append(content[:snippet_chars])
        url = str(item.get("shareLinkedinUrl") or item.get("linkedinUrl") or "").strip()
        if url:
            urls.append(url)
    return " || ".join(snippets), " | ".join(urls)


def select_research_urls(
    homepage_url: str,
    map_payload: dict[str, Any] | None,
    *,
    max_pages: int = 8,
) -> list[str]:
    selected: list[str] = [homepage_url]
    if not isinstance(map_payload, dict):
        return selected
    links = map_payload.get("links")
    if not isinstance(links, list):
        return selected

    seen = {homepage_url.rstrip("/")}
    for item in links:
        candidate = item.get("url") if isinstance(item, dict) else item
        if not isinstance(candidate, str):
            continue
        parsed = urlparse(candidate)
        path = parsed.path.lower()
        if not any(keyword in path for keyword in PAGE_KEYWORDS):
            continue
        normalized = candidate.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(candidate)
        if len(selected) >= max_pages:
            break
    return selected


def guess_research_urls(
    homepage_url: str,
    *,
    max_pages: int = 8,
) -> list[str]:
    parsed = urlparse(homepage_url)
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    selected: list[str] = []
    seen: set[str] = set()
    for path in COMMON_RESEARCH_PATHS:
        candidate = f"{base}/{path}".rstrip("/") if path else homepage_url.rstrip("/")
        if candidate in seen:
            continue
        seen.add(candidate)
        selected.append(candidate if candidate.endswith("/") or path else f"{candidate}/")
        if len(selected) >= max_pages:
            break
    return selected


def brightdata_discovery_seed_urls(homepage_url: str) -> list[str]:
    parsed = urlparse(homepage_url)
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    selected: list[str] = []
    seen: set[str] = set()
    for path in BRIGHTDATA_DISCOVERY_PATHS:
        candidate = f"{base}/{path}".rstrip("/") if path else homepage_url.rstrip("/")
        normalized = candidate if candidate.endswith("/") or path else f"{candidate}/"
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
    return selected


def select_brightdata_research_urls(
    homepage_url: str,
    discovery_pages: list[ResearchPage],
    *,
    max_pages: int = 12,
) -> list[str]:
    homepage_normalized = _normalize_internal_url(homepage_url, homepage_url)
    if not homepage_normalized:
        return []

    candidate_scores: dict[str, int] = {homepage_normalized: 1000}
    for page in discovery_pages:
        for candidate in _extract_internal_urls_from_page(homepage_normalized, page):
            score = _score_research_url(homepage_normalized, candidate)
            if score <= 0:
                continue
            candidate_scores[candidate] = max(candidate_scores.get(candidate, 0), score + 100)

    for candidate in guess_research_urls(homepage_normalized, max_pages=max(max_pages * 2, 12)):
        normalized = _normalize_internal_url(homepage_normalized, candidate)
        if not normalized:
            continue
        score = _score_research_url(homepage_normalized, normalized)
        if score <= 0:
            continue
        candidate_scores[normalized] = max(candidate_scores.get(normalized, 0), score)

    ranked = sorted(
        candidate_scores.items(),
        key=lambda item: (
            -item[1],
            _url_depth(item[0]),
            len(item[0]),
            item[0],
        ),
    )
    return [url for url, _score in ranked[: max(max_pages, 1)]]


def _extract_internal_urls_from_page(homepage_url: str, page: ResearchPage) -> list[str]:
    content = "\n".join(
        value for value in (page.url, page.title, page.description, page.markdown) if value
    )
    return _extract_internal_urls_from_text(homepage_url, content)


def _extract_internal_urls_from_text(homepage_url: str, text: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"https?://[^\s<>()\"']+",
        r"href=[\"']([^\"']+)[\"']",
        r"\[[^\]]+\]\(([^)]+)\)",
        r"<loc>([^<]+)</loc>",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            if isinstance(match, tuple):
                for item in match:
                    if item:
                        candidates.append(item)
            elif match:
                candidates.append(match)

    selected: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_internal_url(homepage_url, candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
    return selected


def _normalize_internal_url(homepage_url: str, candidate: str) -> str:
    raw_value = candidate.strip()
    if not raw_value:
        return ""
    lowered = raw_value.lower()
    if lowered.startswith(EXCLUDED_URL_SCHEMES):
        return ""

    homepage = urlparse(homepage_url if "://" in homepage_url else f"https://{homepage_url}")
    joined = urljoin(f"{homepage.scheme}://{homepage.netloc}/", raw_value)
    parsed = urlparse(joined)
    if not parsed.netloc or parsed.netloc.lower().removeprefix("www.") != homepage.netloc.lower().removeprefix("www."):
        return ""
    if any(parsed.path.lower().endswith(suffix) for suffix in EXCLUDED_URL_SUFFIXES):
        return ""
    if "/wp-json/" in parsed.path.lower() or "/cdn-cgi/" in parsed.path.lower():
        return ""

    normalized_path = parsed.path or "/"
    normalized = f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
    normalized = normalized.rstrip("/") if normalized_path != "/" else normalized.rstrip("/") + "/"
    return normalized


def _url_depth(url: str) -> int:
    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    return len(segments)


def _score_research_url(homepage_url: str, candidate_url: str) -> int:
    if candidate_url == homepage_url:
        return 1000
    parsed = urlparse(candidate_url)
    path = parsed.path.lower().strip("/")
    if not path:
        return 950

    score = 0
    for keyword in PAGE_KEYWORDS:
        if keyword in path:
            score += 40
    for keyword in NEGATIVE_PAGE_KEYWORDS:
        if keyword in path:
            score -= 60
    if _url_depth(candidate_url) <= 2:
        score += 10
    if any(token in path for token in ("about", "service", "solution", "industry", "case", "client", "news", "blog", "team")):
        score += 15
    return score


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value.strip():
            return value.strip()
    return ""


def _find_sentence_block(text: str, patterns: list[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        lowered = sentence.lower()
        if any(pattern in lowered for pattern in patterns):
            return _collapse_whitespace(sentence)[:500]
    return ""


def _find_offer(pages: list[ResearchPage]) -> str:
    candidate = ""
    for page in pages:
        page_hint = f"{page.title}. {page.description}. {page.markdown[:4000]}"
        candidate = _find_sentence_block(
            page_hint,
            [
                "we provide",
                "we help",
                "our services",
                "we offer",
                "managed services",
                "advisory",
                "platform",
                "software",
                "solutions",
            ],
        )
        if candidate:
            return candidate
    return ""


def _find_icp(pages: list[ResearchPage]) -> str:
    for page in pages:
        page_hint = f"{page.title}. {page.description}. {page.markdown[:5000]}"
        candidate = _find_sentence_block(
            page_hint,
            [
                "for ",
                "our clients",
                "our customers",
                "we serve",
                "serving",
                "industries",
                "businesses",
                "companies",
            ],
        )
        if candidate:
            return candidate
    return ""


def _find_painpoint(pages: list[ResearchPage]) -> str:
    for page in pages:
        page_hint = f"{page.title}. {page.description}. {page.markdown[:5000]}"
        candidate = _find_sentence_block(
            page_hint,
            [
                "reduce",
                "avoid",
                "prevent",
                "solve",
                "challenge",
                "bottleneck",
                "improve",
                "grow",
                "protect",
                "streamline",
            ],
        )
        if candidate:
            return candidate
    return ""


def _collect_signal_fragments(
    company_payload: dict[str, Any] | None,
    company_posts_payload: dict[str, Any] | None,
    news_payload: dict[str, Any] | None,
    pages: list[ResearchPage],
) -> tuple[str, str]:
    fragments: list[str] = []
    sources: list[str] = []

    if isinstance(company_payload, dict):
        element = company_payload.get("element")
        if isinstance(element, dict):
            if element.get("jobSearchUrl"):
                fragments.append("Hiring page present on LinkedIn company profile.")
                sources.append("harvest:company.jobSearchUrl")
            if element.get("followerCount"):
                fragments.append(
                    f"LinkedIn follower count: {element.get('followerCount')}."
                )
                sources.append("harvest:company.followerCount")
            crunchbase = element.get("crunchbaseFundingData")
            if isinstance(crunchbase, dict) and crunchbase.get("numberOfFundingRounds"):
                fragments.append(
                    f"Funding rounds visible: {crunchbase.get('numberOfFundingRounds')}."
                )
                sources.append("harvest:company.crunchbaseFundingData")

    if isinstance(company_posts_payload, dict):
        post_summary, post_urls = summarize_company_posts(company_posts_payload, max_posts=2)
        if post_summary:
            fragments.append(f"Recent LinkedIn post: {post_summary[:220]}")
            sources.append("harvest:company-posts")
        if post_urls:
            sources.append(post_urls.split(" | ")[0])

    if isinstance(news_payload, dict):
        news_items = news_payload.get("news")
        if isinstance(news_items, list) and news_items:
            first_news = news_items[0]
            if isinstance(first_news, dict):
                title = _collapse_whitespace(str(first_news.get("title", "")))
                if title:
                    fragments.append(f"Recent news result: {title[:220]}")
                    sources.append("serper:news")

    for page in pages:
        path = urlparse(page.url).path.lower()
        content = f"{page.title}. {page.description}. {page.markdown[:3000]}".lower()
        source_prefix = page.source or "website"
        if "career" in path or "job" in path or "hiring" in content:
            fragments.append("Hiring or careers evidence found on company site.")
            sources.append(f"{source_prefix}:{page.url}")
        if "press" in path or "news" in path or "announc" in content:
            fragments.append("News or announcement evidence found on company site.")
            sources.append(f"{source_prefix}:{page.url}")
        if "portfolio" in path or "investment" in path or "case stud" in content:
            fragments.append("Portfolio, investment, or case-study evidence found on company site.")
            sources.append(f"{source_prefix}:{page.url}")

    unique_fragments: list[str] = []
    seen_fragments: set[str] = set()
    for fragment in fragments:
        if fragment not in seen_fragments:
            unique_fragments.append(fragment)
            seen_fragments.add(fragment)

    unique_sources: list[str] = []
    seen_sources: set[str] = set()
    for source in sources:
        if source not in seen_sources:
            unique_sources.append(source)
            seen_sources.add(source)

    return " | ".join(unique_fragments[:8]), " | ".join(unique_sources[:8])


def summarize_company_research(
    *,
    company_payload: dict[str, Any] | None,
    company_posts_payload: dict[str, Any] | None,
    news_payload: dict[str, Any] | None,
    pages: list[ResearchPage],
) -> CompanyResearchSummary:
    base_description = ""
    if isinstance(company_payload, dict):
        element = company_payload.get("element")
        if isinstance(element, dict):
            base_description = _collapse_whitespace(
                _first_non_empty(
                    str(element.get("description") or ""),
                    str(element.get("tagline") or ""),
                )
            )

    offer = _find_offer(pages)
    icp = _find_icp(pages)
    painpoint = _find_painpoint(pages)
    signals, signal_sources = _collect_signal_fragments(
        company_payload,
        company_posts_payload,
        news_payload,
        pages,
    )
    description = _first_non_empty(
        base_description,
        _first_non_empty(*(page.description for page in pages)),
    )
    return CompanyResearchSummary(
        description=description,
        offer=offer,
        icp=icp,
        painpoint=painpoint,
        signals=signals,
        signal_sources=signal_sources,
    )
