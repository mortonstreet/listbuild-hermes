from __future__ import annotations

from typing import Any

from listbuild.config import FirecrawlSettings
from listbuild.http import ApiResponse, BaseApiClient, TokenBucket


class FirecrawlClient:
    """
    Firecrawl client.

    Public source used for this wrapper:
    - https://docs.firecrawl.dev/introduction
    """

    def __init__(self, settings: FirecrawlSettings) -> None:
        self._scrape_limiter = TokenBucket(
            rate_per_second=settings.scrape_qps,
            burst=settings.scrape_burst,
        )
        self._map_limiter = TokenBucket(
            rate_per_second=settings.map_qps,
            burst=settings.map_burst,
        )
        self._search_limiter = TokenBucket(
            rate_per_second=settings.search_qps,
            burst=settings.search_burst,
        )
        self._client = BaseApiClient(
            provider="firecrawl",
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
            default_headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.api_key}",
            },
        )

    async def __aenter__(self) -> "FirecrawlClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def raw(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        category: str = "scrape",
    ) -> ApiResponse:
        return await self._client.request(
            method,
            path,
            limiter=self._select_limiter(category),
            json_body=payload,
        )

    async def scrape(
        self,
        url: str,
        *,
        formats: list[str] | None = None,
        only_main_content: bool | None = None,
        timeout: int | None = None,
        wait_for: int | None = None,
        max_age: int | None = None,
        country: str | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"url": url}
        if formats:
            payload["formats"] = formats
        if only_main_content is not None:
            payload["onlyMainContent"] = only_main_content
        if timeout is not None:
            payload["timeout"] = timeout
        if wait_for is not None:
            payload["waitFor"] = wait_for
        if max_age is not None:
            payload["maxAge"] = max_age
        if country is not None:
            payload["country"] = country
        return await self.raw("POST", "/v2/scrape", payload=payload, category="scrape")

    async def map(
        self,
        url: str,
        *,
        search: str | None = None,
        limit: int | None = None,
        include_subdomains: bool | None = None,
        sitemap: str | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"url": url}
        if search is not None:
            payload["search"] = search
        if limit is not None:
            payload["limit"] = limit
        if include_subdomains is not None:
            payload["includeSubdomains"] = include_subdomains
        if sitemap is not None:
            payload["sitemap"] = sitemap
        return await self.raw("POST", "/v2/map", payload=payload, category="map")

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        country: str | None = None,
        location: str | None = None,
        tbs: str | None = None,
        sources: list[str] | None = None,
        scrape_options: dict[str, Any] | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"query": query}
        if limit is not None:
            payload["limit"] = limit
        if country is not None:
            payload["country"] = country
        if location is not None:
            payload["location"] = location
        if tbs is not None:
            payload["tbs"] = tbs
        if sources:
            payload["sources"] = sources
        if scrape_options:
            payload["scrapeOptions"] = scrape_options
        return await self.raw("POST", "/v2/search", payload=payload, category="search")

    def _select_limiter(self, category: str) -> TokenBucket:
        normalized = category.lower()
        if normalized == "map":
            return self._map_limiter
        if normalized == "search":
            return self._search_limiter
        return self._scrape_limiter
