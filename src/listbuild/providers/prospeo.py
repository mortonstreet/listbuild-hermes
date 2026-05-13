from __future__ import annotations

from typing import Any

from listbuild.config import ProspeoSettings
from listbuild.http import ApiResponse, BaseApiClient, TokenBucket


class ProspeoClient:
    """
    Prospeo client.

    Public source used for this wrapper:
    - https://prospeo.io/api-docs
    """

    def __init__(self, settings: ProspeoSettings) -> None:
        self._search_limiter = TokenBucket(
            rate_per_second=settings.search_qps,
            burst=settings.search_burst,
        )
        self._enrich_limiter = TokenBucket(
            rate_per_second=settings.enrich_qps,
            burst=settings.enrich_burst,
        )
        self._client = BaseApiClient(
            provider="prospeo",
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
            default_headers={
                "Content-Type": "application/json",
                "X-KEY": settings.api_key,
            },
        )

    async def __aenter__(self) -> "ProspeoClient":
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
        category: str = "search",
    ) -> ApiResponse:
        limiter = self._select_limiter(category)
        return await self._client.request(
            method,
            path,
            limiter=limiter,
            json_body=payload,
        )

    async def account_information(self) -> ApiResponse:
        return await self._client.request(
            "GET",
            "/account-information",
            limiter=self._enrich_limiter,
        )

    async def search_person(self, payload: dict[str, Any]) -> ApiResponse:
        return await self.raw(
            "POST",
            "/search-person",
            payload=payload,
            category="search",
        )

    async def search_company(self, payload: dict[str, Any]) -> ApiResponse:
        return await self.raw(
            "POST",
            "/search-company",
            payload=payload,
            category="search",
        )

    async def enrich_person(self, payload: dict[str, Any]) -> ApiResponse:
        return await self.raw(
            "POST",
            "/enrich-person",
            payload=payload,
            category="enrich",
        )

    async def enrich_company(self, payload: dict[str, Any]) -> ApiResponse:
        return await self.raw(
            "POST",
            "/enrich-company",
            payload=payload,
            category="enrich",
        )

    def _select_limiter(self, category: str) -> TokenBucket:
        normalized = category.lower()
        if normalized == "enrich":
            return self._enrich_limiter
        return self._search_limiter
