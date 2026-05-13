from __future__ import annotations

import asyncio
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
from time import monotonic
import threading
from typing import Any, Mapping
import uuid

import httpx

from listbuild.jsonl import append_jsonl


_AUDIT_WRITE_LOCK = threading.Lock()
_AUDIT_REDACTED_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
}


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sanitize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if headers is None:
        return {}
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).lower()
        sanitized[str(key)] = "[redacted]" if lowered in _AUDIT_REDACTED_HEADERS else str(value)
    return sanitized


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _parse_number(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    stripped = value.strip()
    try:
        return max(float(stripped), 0.0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            return None
        now = datetime.now(retry_at.tzinfo)
        return max((retry_at - now).total_seconds(), 0.0)


@dataclass(slots=True)
class RateLimitSnapshot:
    retry_after_seconds: float | None = None
    per_second_limit: int | None = None
    per_minute_limit: int | None = None
    per_day_limit: int | None = None
    per_second_remaining: int | None = None
    per_minute_remaining: int | None = None
    per_day_remaining: int | None = None
    minute_reset_seconds: int | None = None
    daily_reset_seconds: int | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "RateLimitSnapshot":
        normalized = {key.lower(): value for key, value in headers.items()}
        return cls(
            retry_after_seconds=_parse_retry_after(normalized.get("retry-after")),
            per_second_limit=_parse_number(normalized.get("x-second-rate-limit")),
            per_minute_limit=_parse_number(normalized.get("x-minute-rate-limit")),
            per_day_limit=_parse_number(normalized.get("x-daily-rate-limit")),
            per_second_remaining=_parse_number(normalized.get("x-second-rate-limit-remaining")),
            per_minute_remaining=_parse_number(
                normalized.get("x-minute-request-left")
                or normalized.get("x-minute-rate-limit-remaining")
            ),
            per_day_remaining=_parse_number(normalized.get("x-daily-request-left")),
            minute_reset_seconds=_parse_number(normalized.get("x-minute-reset-seconds")),
            daily_reset_seconds=_parse_number(normalized.get("x-daily-reset-seconds")),
        )


@dataclass(slots=True)
class ApiResponse:
    provider: str
    status_code: int
    data: Any
    headers: dict[str, str]
    rate_limit: RateLimitSnapshot


class ApiError(RuntimeError):
    def __init__(
        self,
        provider: str,
        status_code: int,
        message: str,
        response_text: str,
    ) -> None:
        super().__init__(f"{provider} returned HTTP {status_code}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.message = message
        self.response_text = response_text

    @classmethod
    def from_response(cls, provider: str, response: httpx.Response) -> "ApiError":
        response_text = response.text
        message = response.reason_phrase
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            message = (
                payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or payload.get("status")
                or message
            )
        return cls(
            provider=provider,
            status_code=response.status_code,
            message=str(message),
            response_text=response_text,
        )


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate_per_second = max(rate_per_second, 0.0)
        self._burst = max(burst, 1)
        self._tokens = float(self._burst)
        self._updated_at = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._rate_per_second <= 0:
            return

        while True:
            async with self._lock:
                now = monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(
                    float(self._burst),
                    self._tokens + elapsed * self._rate_per_second,
                )
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                sleep_for = (1 - self._tokens) / self._rate_per_second
            await asyncio.sleep(sleep_for)

    def update_rate(self, rate_per_second: float, burst: int | None = None) -> None:
        now = monotonic()
        elapsed = now - self._updated_at
        self._tokens = min(
            float(self._burst),
            self._tokens + elapsed * self._rate_per_second,
        )
        self._updated_at = now
        self._rate_per_second = max(rate_per_second, 0.0)
        if burst is not None:
            self._burst = max(burst, 1)
        self._tokens = min(self._tokens, float(self._burst))


class ApiKeyPool:
    def __init__(self, keys: tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("ApiKeyPool requires at least one key")
        self._keys = keys
        self._index = 0

    def next(self) -> str:
        key = self._keys[self._index]
        self._index = (self._index + 1) % len(self._keys)
        return key


class BaseApiClient:
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        provider: str,
        base_url: str,
        timeout_seconds: float,
        max_connections: int,
        default_headers: Mapping[str, str] | None = None,
        max_retries: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max(1, max_connections // 2),
        )
        self.provider = provider
        self.max_retries = max_retries
        audit_flag = os.environ.get("LISTBUILD_API_AUDIT", "1").strip().lower()
        audit_enabled = audit_flag not in {"0", "false", "no", "off"}
        audit_path_raw = os.environ.get("LISTBUILD_API_AUDIT_LOG", "").strip()
        self._audit_path = None
        if audit_enabled:
            self._audit_path = Path(audit_path_raw).expanduser() if audit_path_raw else Path.cwd() / "api-call-audit.jsonl"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            follow_redirects=True,
            limits=limits,
            headers=default_headers,
        )

    async def __aenter__(self) -> "BaseApiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        limiter: TokenBucket | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        files: Any = None,
        parse_as: str = "json",
    ) -> ApiResponse:
        path = path if path.startswith("/") else f"/{path}"
        method_name = method.upper()
        request_id = uuid.uuid4().hex
        request_headers = _sanitize_headers(headers)
        request_params = _json_safe(dict(params)) if params is not None else {}
        request_json = _json_safe(dict(json_body)) if json_body is not None else {}

        for attempt in range(self.max_retries + 1):
            started_at = _utc_timestamp()
            started_monotonic = monotonic()
            if limiter is not None:
                await limiter.acquire()

            try:
                response = await self._client.request(
                    method=method_name,
                    url=path,
                    params=params,
                    json=json_body,
                    headers=headers,
                    files=files,
                )
            except Exception as exc:  # noqa: BLE001
                self._audit_event(
                    request_id=request_id,
                    attempt=attempt + 1,
                    method=method_name,
                    path=path,
                    started_at=started_at,
                    completed_at=_utc_timestamp(),
                    duration_ms=int((monotonic() - started_monotonic) * 1000),
                    outcome="transport_error",
                    request_headers=request_headers,
                    request_params=request_params,
                    request_json=request_json,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                raise

            snapshot = RateLimitSnapshot.from_headers(response.headers)
            completed_at = _utc_timestamp()
            duration_ms = int((monotonic() - started_monotonic) * 1000)

            if limiter is not None and snapshot.per_second_limit:
                limiter.update_rate(
                    rate_per_second=float(snapshot.per_second_limit),
                    burst=max(snapshot.per_second_limit, 1),
                )

            if (
                response.status_code in self.RETRYABLE_STATUS_CODES
                and attempt < self.max_retries
            ):
                delay = snapshot.retry_after_seconds
                if delay is None:
                    delay = min(2**attempt, 16)
                self._audit_event(
                    request_id=request_id,
                    attempt=attempt + 1,
                    method=method_name,
                    path=path,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    outcome="retryable_status",
                    request_headers=request_headers,
                    request_params=request_params,
                    request_json=request_json,
                    status_code=response.status_code,
                    response_headers=dict(response.headers),
                    rate_limit=asdict(snapshot),
                    retry_scheduled_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue

            if not response.is_success:
                self._audit_event(
                    request_id=request_id,
                    attempt=attempt + 1,
                    method=method_name,
                    path=path,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    outcome="http_error",
                    request_headers=request_headers,
                    request_params=request_params,
                    request_json=request_json,
                    status_code=response.status_code,
                    response_headers=dict(response.headers),
                    rate_limit=asdict(snapshot),
                    response_text_excerpt=response.text[:2000],
                )
                raise ApiError.from_response(self.provider, response)

            decoded = self._decode_response(response, parse_as=parse_as)
            self._audit_event(
                request_id=request_id,
                attempt=attempt + 1,
                method=method_name,
                path=path,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                outcome="success",
                request_headers=request_headers,
                request_params=request_params,
                request_json=request_json,
                status_code=response.status_code,
                response_headers=dict(response.headers),
                rate_limit=asdict(snapshot),
            )
            return ApiResponse(
                provider=self.provider,
                status_code=response.status_code,
                data=decoded,
                headers=dict(response.headers),
                rate_limit=snapshot,
            )

        raise RuntimeError(f"Unreachable retry loop in {self.provider} client")

    def _audit_event(
        self,
        *,
        request_id: str,
        attempt: int,
        method: str,
        path: str,
        started_at: str,
        completed_at: str,
        duration_ms: int,
        outcome: str,
        request_headers: Mapping[str, Any],
        request_params: Any,
        request_json: Any,
        status_code: int | None = None,
        response_headers: Mapping[str, Any] | None = None,
        rate_limit: Mapping[str, Any] | None = None,
        retry_scheduled_seconds: float | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        response_text_excerpt: str | None = None,
    ) -> None:
        if self._audit_path is None:
            return
        payload = {
            "logged_at": _utc_timestamp(),
            "request_id": request_id,
            "provider": self.provider,
            "base_url": self.base_url,
            "method": method,
            "path": path,
            "attempt": attempt,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "status_code": status_code,
            "request_headers": _sanitize_headers(request_headers),
            "request_params": request_params,
            "request_json": request_json,
            "response_headers": dict(response_headers or {}),
            "rate_limit": dict(rate_limit or {}),
            "retry_scheduled_seconds": retry_scheduled_seconds,
            "error_type": error_type or "",
            "error_message": error_message or "",
            "response_text_excerpt": response_text_excerpt or "",
        }
        with _AUDIT_WRITE_LOCK:
            append_jsonl(self._audit_path, payload)

    @staticmethod
    def _decode_response(response: httpx.Response, *, parse_as: str) -> Any:
        if parse_as == "bytes":
            return response.content
        if parse_as == "text":
            return response.text

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type or "text/json" in content_type:
            return response.json()

        try:
            return response.json()
        except json.JSONDecodeError:
            return {"raw_text": response.text}
