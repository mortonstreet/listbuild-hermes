from __future__ import annotations

from typing import Any

from listbuild.config import MiniMaxSettings
from listbuild.http import ApiResponse, BaseApiClient, TokenBucket


class MiniMaxClient:
    """
    MiniMax text-generation client.

    Public source used for this wrapper:
    - https://platform.minimax.io/docs/api-reference/text-post
    """

    def __init__(self, settings: MiniMaxSettings) -> None:
        self._settings = settings
        self._limiter = TokenBucket(
            rate_per_second=settings.requests_per_second,
            burst=settings.burst,
        )
        self._client = BaseApiClient(
            provider="minimax",
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_connections=settings.max_connections,
            default_headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "MiniMaxClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        max_completion_tokens: int | None = None,
        top_p: float = 0.95,
        response_format: dict[str, Any] | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {
            "model": model or self._settings.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
        }
        if max_completion_tokens is not None:
            payload["max_completion_tokens"] = max_completion_tokens
        else:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        return await self._client.request(
            "POST",
            "/v1/text/chatcompletion_v2",
            limiter=self._limiter,
            json_body=payload,
        )
