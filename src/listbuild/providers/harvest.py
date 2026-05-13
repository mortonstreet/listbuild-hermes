from __future__ import annotations

from typing import Any

from listbuild.config import ProviderSettings
from listbuild.http import ApiResponse, BaseApiClient, TokenBucket


class HarvestClient:
    """
    Harvest API client.

    Public source used for this wrapper:
    - https://docs.harvest-api.com/guides/quickstart
    """

    def __init__(self, settings: ProviderSettings) -> None:
        self._api_key = settings.api_keys[0]
        self._limiter = TokenBucket(
            rate_per_second=settings.requests_per_second,
            burst=settings.burst,
        )
        self._client = BaseApiClient(
            provider=settings.name,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
            default_headers={"X-API-Key": self._api_key},
        )

    async def __aenter__(self) -> "HarvestClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def raw(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> ApiResponse:
        return await self._client.request(
            "GET",
            path,
            limiter=self._limiter,
            params=params,
        )

    async def company_search(self, **params: Any) -> ApiResponse:
        return await self.raw("/linkedin/company-search", params=params)

    async def profile_search(self, **params: Any) -> ApiResponse:
        return await self.raw("/linkedin/profile-search", params=params)

    async def geo_id_search(self, search: str) -> ApiResponse:
        return await self.raw("/linkedin/geo-id-search", params={"search": search})

    async def get_company(
        self,
        *,
        url: str | None = None,
        universal_name: str | None = None,
        search: str | None = None,
    ) -> ApiResponse:
        params: dict[str, Any] = {}
        if url is not None:
            params["url"] = url
        if universal_name is not None:
            params["universalName"] = universal_name
        if search is not None:
            params["search"] = search
        return await self.raw("/linkedin/company", params=params)

    async def get_profile(
        self,
        *,
        url: str | None = None,
        public_identifier: str | None = None,
        profile_id: str | None = None,
        main: bool | None = None,
        find_email: bool | None = None,
        skip_smtp: bool | None = None,
        include_about_profile: bool | None = None,
    ) -> ApiResponse:
        params: dict[str, Any] = {}
        if url is not None:
            params["url"] = url
        if public_identifier is not None:
            params["publicIdentifier"] = public_identifier
        if profile_id is not None:
            params["profileId"] = profile_id
        if main is not None:
            params["main"] = str(main).lower()
        if find_email is not None:
            params["findEmail"] = str(find_email).lower()
        if skip_smtp is not None:
            params["skipSmtp"] = str(skip_smtp).lower()
        if include_about_profile is not None:
            params["includeAboutProfile"] = str(include_about_profile).lower()
        return await self.raw("/linkedin/profile", params=params)

    async def company_posts(
        self,
        *,
        company: str | None = None,
        company_id: str | None = None,
        company_universal_name: str | None = None,
        posted_limit: str | None = None,
        scrape_posted_limit: str | None = None,
        page: int | None = None,
        pagination_token: str | None = None,
    ) -> ApiResponse:
        params: dict[str, Any] = {}
        if company is not None:
            params["company"] = company
        if company_id is not None:
            params["companyId"] = company_id
        if company_universal_name is not None:
            params["companyUniversalName"] = company_universal_name
        if posted_limit is not None:
            params["postedLimit"] = posted_limit
        if scrape_posted_limit is not None:
            params["scrapePostedLimit"] = scrape_posted_limit
        if page is not None:
            params["page"] = page
        if pagination_token is not None:
            params["paginationToken"] = pagination_token
        return await self.raw("/linkedin/company-posts", params=params)

    async def profile_posts(
        self,
        *,
        profile: str | None = None,
        profile_id: str | None = None,
        profile_public_identifier: str | None = None,
        posted_limit: str | None = None,
        scrape_posted_limit: str | None = None,
        page: int | None = None,
        pagination_token: str | None = None,
    ) -> ApiResponse:
        params: dict[str, Any] = {}
        if profile is not None:
            params["profile"] = profile
        if profile_id is not None:
            params["profileId"] = profile_id
        if profile_public_identifier is not None:
            params["profilePublicIdentifier"] = profile_public_identifier
        if posted_limit is not None:
            params["postedLimit"] = posted_limit
        if scrape_posted_limit is not None:
            params["scrapePostedLimit"] = scrape_posted_limit
        if page is not None:
            params["page"] = page
        if pagination_token is not None:
            params["paginationToken"] = pagination_token
        return await self.raw("/linkedin/profile-posts", params=params)
