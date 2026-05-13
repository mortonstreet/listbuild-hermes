from __future__ import annotations

import asyncio
from typing import Any

from listbuild.config import BrightDataSettings
from listbuild.http import ApiResponse, BaseApiClient, TokenBucket


class BrightDataClient:
    """
    Bright Data Web Scraper / Crawl client.

    Public sources used for this wrapper:
    - https://docs.brightdata.com/scraping-automation/crawl-api/quick-start
    - https://docs.brightdata.com/scraping-automation/web-scraper-api/overview
    """

    def __init__(self, settings: BrightDataSettings) -> None:
        self._settings = settings
        self._limiter = TokenBucket(
            rate_per_second=settings.requests_per_second,
            burst=settings.burst,
        )
        self._client = BaseApiClient(
            provider="brightdata",
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
            default_headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "BrightDataClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def scrape_urls(
        self,
        urls: list[str],
        *,
        custom_output_fields: tuple[str, ...] = ("markdown", "html2text", "page_html"),
    ) -> ApiResponse:
        payload = {
            "input": [{"url": url} for url in urls],
            "custom_output_fields": "|".join(custom_output_fields),
        }
        response = await self._client.request(
            "POST",
            "/datasets/v3/scrape",
            limiter=self._limiter,
            params={
                "dataset_id": self._settings.crawl_dataset_id,
                "format": "json",
            },
            json_body=payload,
        )
        if response.status_code != 202:
            return response

        snapshot_id = self._extract_snapshot_id(response.data)
        if not snapshot_id:
            return response
        return await self._wait_for_snapshot(snapshot_id)

    async def trigger_urls(
        self,
        urls: list[str],
        *,
        custom_output_fields: tuple[str, ...] = ("markdown", "html2text", "page_html"),
        include_errors: bool = True,
    ) -> ApiResponse:
        response = await self._client.request(
            "POST",
            "/datasets/v3/trigger",
            limiter=self._limiter,
            params={
                "dataset_id": self._settings.crawl_dataset_id,
                "format": "json",
                "include_errors": str(include_errors).lower(),
                "custom_output_fields": "|".join(custom_output_fields),
            },
            json_body=[{"url": url} for url in urls],
        )
        return response

    async def progress(self, snapshot_id: str) -> ApiResponse:
        return await self._client.request(
            "GET",
            f"/datasets/v3/progress/{snapshot_id}",
            limiter=self._limiter,
        )

    async def download_snapshot(self, snapshot_id: str) -> ApiResponse:
        return await self._client.request(
            "GET",
            f"/datasets/v3/snapshot/{snapshot_id}",
            limiter=self._limiter,
            params={"format": "json"},
        )

    async def _wait_for_snapshot(self, snapshot_id: str) -> ApiResponse:
        deadline = asyncio.get_running_loop().time() + self._settings.poll_timeout_seconds
        while True:
            progress = await self.progress(snapshot_id)
            payload = progress.data if isinstance(progress.data, dict) else {}
            status = str(payload.get("status") or "").lower()
            if status in {"ready", "completed", "done", "success"}:
                return await self.download_snapshot(snapshot_id)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Bright Data snapshot {snapshot_id} did not complete within {self._settings.poll_timeout_seconds} seconds"
                )
            await asyncio.sleep(self._settings.poll_interval_seconds)

    @staticmethod
    def _extract_snapshot_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        return str(
            payload.get("snapshot_id")
            or payload.get("snapshotId")
            or payload.get("id")
            or ""
        ).strip()
