from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from listbuild.config import AppSettings
from listbuild.normalize import is_discovery_host_allowed, normalize_url
from listbuild.providers import FirecrawlClient, SerperClient


@dataclass(frozen=True, slots=True)
class CompanyDiscoveryPolicy:
    search_results: int = 20
    max_companies: int = 10
    scrape_homepages: bool = True
    scrape_formats: tuple[str, ...] = ("markdown",)
    scrape_only_main_content: bool = True


@dataclass(slots=True)
class DiscoveryEvidence:
    position: int
    url: str
    title: str | None = None
    snippet: str | None = None


@dataclass(slots=True)
class ScrapeSnapshot:
    title: str | None = None
    description: str | None = None
    markdown: str | None = None
    html: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class CompanyCandidate:
    host: str
    homepage_url: str
    evidence: list[DiscoveryEvidence] = field(default_factory=list)
    scrape: ScrapeSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "homepage_url": self.homepage_url,
            "evidence": [asdict(item) for item in self.evidence],
            "scrape": asdict(self.scrape) if self.scrape is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CompanyDiscoveryDecision:
    use_serper_search: bool
    use_firecrawl_scrape: bool
    reason: str


@dataclass(slots=True)
class CompanyDiscoveryRun:
    query: str
    policy: CompanyDiscoveryPolicy
    decision: CompanyDiscoveryDecision
    candidates: list[CompanyCandidate]
    raw_result_count: int
    accepted_result_count: int
    filtered_result_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "policy": asdict(self.policy),
            "decision": asdict(self.decision),
            "raw_result_count": self.raw_result_count,
            "accepted_result_count": self.accepted_result_count,
            "filtered_result_count": self.filtered_result_count,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class CompanyDiscoveryWorkflow:
    """
    Deterministic first-stage workflow:
    1. Serper discovers candidate company domains from a query.
    2. Firecrawl optionally scrapes the accepted homepages.
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    async def run_query(
        self,
        query: str,
        *,
        policy: CompanyDiscoveryPolicy | None = None,
    ) -> CompanyDiscoveryRun:
        policy = policy or CompanyDiscoveryPolicy()
        decision = self._decide_for_query(query=query, policy=policy)

        async with SerperClient(self._settings.serper) as serper:
            search_response = await serper.search(query, num=policy.search_results)

        raw_pages = self._extract_serper_pages(search_response.data)
        candidates, filtered_count = self._build_candidates(
            raw_pages,
            max_companies=policy.max_companies,
        )

        if decision.use_firecrawl_scrape and candidates:
            async with FirecrawlClient(self._settings.firecrawl) as firecrawl:
                await self._scrape_candidates(
                    firecrawl=firecrawl,
                    candidates=candidates,
                    policy=policy,
                )

        return CompanyDiscoveryRun(
            query=query,
            policy=policy,
            decision=decision,
            candidates=candidates,
            raw_result_count=len(raw_pages),
            accepted_result_count=len(candidates),
            filtered_result_count=filtered_count,
        )

    @staticmethod
    def _decide_for_query(
        *,
        query: str,
        policy: CompanyDiscoveryPolicy,
    ) -> CompanyDiscoveryDecision:
        normalized = query.strip()
        if not normalized:
            raise ValueError("Company discovery query cannot be empty")

        return CompanyDiscoveryDecision(
            use_serper_search=True,
            use_firecrawl_scrape=policy.scrape_homepages,
            reason=(
                "Use Serper first for cheap high-throughput discovery, then scrape only "
                "deduplicated first-party homepages through Firecrawl."
            ),
        )

    @staticmethod
    def _extract_serper_pages(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []

        organic = payload.get("organic")
        if not isinstance(organic, list):
            return []

        pages: list[dict[str, Any]] = []
        for index, item in enumerate(organic, start=1):
            if not isinstance(item, dict):
                continue
            url = item.get("link") or item.get("url")
            if not isinstance(url, str):
                continue
            pages.append(
                {
                    "position": index,
                    "url": url,
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                }
            )
        return pages

    @staticmethod
    def _build_candidates(
        pages: list[dict[str, Any]],
        *,
        max_companies: int,
    ) -> tuple[list[CompanyCandidate], int]:
        candidates_by_host: dict[str, CompanyCandidate] = {}
        filtered_count = 0

        for page in pages:
            normalized = normalize_url(str(page["url"]))
            if normalized is None:
                filtered_count += 1
                continue
            if not is_discovery_host_allowed(normalized.host):
                filtered_count += 1
                continue

            candidate = candidates_by_host.get(normalized.host)
            if candidate is None:
                if len(candidates_by_host) >= max_companies:
                    break
                candidate = CompanyCandidate(
                    host=normalized.host,
                    homepage_url=normalized.homepage_url,
                )
                candidates_by_host[normalized.host] = candidate

            candidate.evidence.append(
                DiscoveryEvidence(
                    position=int(page["position"]),
                    url=normalized.normalized_url,
                    title=page.get("title"),
                    snippet=page.get("snippet"),
                )
            )

        return list(candidates_by_host.values()), filtered_count

    async def _scrape_candidates(
        self,
        *,
        firecrawl: FirecrawlClient,
        candidates: list[CompanyCandidate],
        policy: CompanyDiscoveryPolicy,
    ) -> None:
        async def scrape_one(candidate: CompanyCandidate) -> None:
            response = await firecrawl.scrape(
                candidate.homepage_url,
                formats=list(policy.scrape_formats),
                only_main_content=policy.scrape_only_main_content,
            )
            candidate.scrape = self._snapshot_from_firecrawl_response(response.data)

        await asyncio.gather(*(scrape_one(candidate) for candidate in candidates))

    @staticmethod
    def _snapshot_from_firecrawl_response(payload: Any) -> ScrapeSnapshot:
        if not isinstance(payload, dict):
            return ScrapeSnapshot()

        data = payload.get("data")
        if isinstance(data, dict):
            payload = data

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = None

        title = None
        description = None
        if metadata is not None:
            title = metadata.get("title")
            description = metadata.get("description")

        return ScrapeSnapshot(
            title=title,
            description=description,
            markdown=payload.get("markdown") if isinstance(payload.get("markdown"), str) else None,
            html=payload.get("html") if isinstance(payload.get("html"), str) else None,
            metadata=metadata,
        )
