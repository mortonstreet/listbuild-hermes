from __future__ import annotations

from pathlib import Path

from listbuild.config import ProviderSettings
from listbuild.http import ApiResponse, BaseApiClient, TokenBucket


class MillionVerifierClient:
    """
    MillionVerifier client.

    Public sources used for this wrapper:
    - https://developer.millionverifier.com/
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
        )
        self._bulk_client = BaseApiClient(
            provider=f"{settings.name}_bulk",
            base_url="https://bulkapi.millionverifier.com",
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
        )

    async def __aenter__(self) -> "MillionVerifierClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._bulk_client.aclose()

    async def verify_email(self, email: str, timeout: int = 20) -> ApiResponse:
        return await self._client.request(
            "GET",
            "/api/v3",
            limiter=self._limiter,
            params={
                "api": self._api_key,
                "email": email,
                "timeout": timeout,
            },
        )

    async def credits(self) -> ApiResponse:
        return await self._client.request(
            "GET",
            "/api/v3/credits",
            limiter=self._limiter,
            params={"api": self._api_key},
        )

    async def upload_file(self, file_path: str) -> ApiResponse:
        target = Path(file_path)
        with target.open("rb") as handle:
            return await self._bulk_client.request(
                "POST",
                "/bulkapi/v2/upload",
                limiter=self._limiter,
                params={"key": self._api_key},
                files={"file_contents": (target.name, handle, "text/csv")},
            )

    async def bulk_file_info(self, file_id: str) -> ApiResponse:
        return await self._bulk_client.request(
            "GET",
            "/bulkapi/v2/fileinfo",
            limiter=self._limiter,
            params={"key": self._api_key, "file_id": file_id},
        )

    async def download_report(
        self,
        file_id: str,
        *,
        filter_status: str = "all",
        statuses: str | None = None,
        free: bool | None = None,
        role: bool | None = None,
    ) -> ApiResponse:
        params = {
            "key": self._api_key,
            "file_id": file_id,
            "filter": filter_status,
        }
        if statuses is not None:
            params["statuses"] = statuses
        if free is not None:
            params["free"] = "1" if free else "0"
        if role is not None:
            params["role"] = "1" if role else "0"
        return await self._bulk_client.request(
            "GET",
            "/bulkapi/v2/download",
            limiter=self._limiter,
            params=params,
            parse_as="bytes",
        )

    async def stop_bulk_file(self, file_id: str) -> ApiResponse:
        return await self._bulk_client.request(
            "GET",
            "/bulkapi/stop",
            limiter=self._limiter,
            params={"key": self._api_key, "file_id": file_id},
        )

    async def delete_bulk_file(self, file_id: str) -> ApiResponse:
        return await self._bulk_client.request(
            "GET",
            "/bulkapi/v2/delete",
            limiter=self._limiter,
            params={"key": self._api_key, "file_id": file_id},
        )
