from __future__ import annotations

from typing import Any

from listbuild.config import ProviderSettings
from listbuild.http import ApiKeyPool, ApiResponse, BaseApiClient, TokenBucket


class SerperClient:
    """
    Serper.dev client.

    Public source used for this wrapper:
    - https://serper.dev/

    The public site documents the available search products and pricing tiers.
    The request contract here follows the current Serper endpoint structure
    used by their public API surface: POST https://google.serper.dev/{type}
    with an X-API-KEY header.
    """

    def __init__(self, settings: ProviderSettings) -> None:
        self._key_pool = ApiKeyPool(settings.api_keys)
        self._limiter = TokenBucket(
            rate_per_second=settings.requests_per_second,
            burst=settings.burst,
        )
        self._client = BaseApiClient(
            provider=settings.name,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
            default_headers={"Content-Type": "application/json"},
        )

    async def __aenter__(self) -> "SerperClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def raw(self, endpoint: str, payload: dict[str, Any]) -> ApiResponse:
        return await self._client.request(
            "POST",
            endpoint,
            limiter=self._limiter,
            headers={"X-API-KEY": self._key_pool.next()},
            json_body=payload,
        )

    async def search(
        self,
        query: str,
        *,
        gl: str | None = None,
        hl: str | None = None,
        num: int | None = None,
        page: int | None = None,
        autocorrect: bool | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"q": query}
        if gl is not None:
            payload["gl"] = gl
        if hl is not None:
            payload["hl"] = hl
        if num is not None:
            payload["num"] = num
        if page is not None:
            payload["page"] = page
        if autocorrect is not None:
            payload["autocorrect"] = autocorrect
        return await self.raw("search", payload)

    async def news(
        self,
        query: str,
        *,
        gl: str | None = None,
        hl: str | None = None,
        num: int | None = None,
        page: int | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"q": query}
        if gl is not None:
            payload["gl"] = gl
        if hl is not None:
            payload["hl"] = hl
        if num is not None:
            payload["num"] = num
        if page is not None:
            payload["page"] = page
        return await self.raw("news", payload)

    async def images(
        self,
        query: str,
        *,
        gl: str | None = None,
        hl: str | None = None,
        num: int | None = None,
        page: int | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"q": query}
        if gl is not None:
            payload["gl"] = gl
        if hl is not None:
            payload["hl"] = hl
        if num is not None:
            payload["num"] = num
        if page is not None:
            payload["page"] = page
        return await self.raw("images", payload)

    async def places(
        self,
        query: str,
        *,
        gl: str | None = None,
        hl: str | None = None,
        page: int | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"q": query}
        if gl is not None:
            payload["gl"] = gl
        if hl is not None:
            payload["hl"] = hl
        if page is not None:
            payload["page"] = page
        return await self.raw("places", payload)
