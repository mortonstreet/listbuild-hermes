from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any

from listbuild.jsonl import append_jsonl


def _round_money(value: float) -> float:
    return round(value, 6)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime | None:
    stripped = (value or "").strip()
    if not stripped:
        return None
    try:
        return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(started_at: str, completed_at: str) -> float | None:
    started = _parse_utc_timestamp(started_at)
    completed = _parse_utc_timestamp(completed_at)
    if started is None or completed is None:
        return None
    return max((completed - started).total_seconds(), 0.0)


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total_ms = max(int(round(seconds * 1000)), 0)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _rate_per_minute(count: int | float, seconds: float | None) -> float:
    if seconds is None or seconds <= 0:
        return 0.0
    return round(float(count) / (seconds / 60.0), 6)


@dataclass(frozen=True, slots=True)
class ScrapePricing:
    serper_search_usd: float = 0.003
    serper_news_usd: float = 0.003
    harvest_company_get_usd: float = 0.002
    harvest_company_posts_usd: float = 0.002
    harvest_full_profile_usd: float = 0.0032
    harvest_main_profile_usd: float = 0.002
    harvest_profile_with_email_usd: float = 0.01
    harvest_profile_posts_usd: float = 0.002
    minimax_company_reasoning_usd: float = 0.0
    brightdata_record_usd: float = 0.0015
    firecrawl_credit_usd: float = 0.001
    firecrawl_map_credits_per_request: float = 1.0
    firecrawl_scrape_credits_per_page: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScrapePricing":
        payload = dict(asdict(cls()))
        for key in payload:
            if key not in data:
                continue
            value = data[key]
            if not isinstance(value, (int, float)):
                raise ValueError(f"Expected {key} to be numeric")
            payload[key] = float(value)
        return cls(**payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "ScrapePricing":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Pricing file must contain a JSON object")
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, float]:
        return {key: float(value) for key, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    recorded_at: str
    provider: str
    stage: str
    operation: str
    calls: int
    usage_units: float
    usage_label: str
    unit_cost_usd: float
    total_cost_usd: float
    cumulative_cost_usd: float
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at,
            "provider": self.provider,
            "stage": self.stage,
            "operation": self.operation,
            "calls": self.calls,
            "usage_units": round(self.usage_units, 6),
            "usage_label": self.usage_label,
            "unit_cost_usd": _round_money(self.unit_cost_usd),
            "total_cost_usd": _round_money(self.total_cost_usd),
            "cumulative_cost_usd": _round_money(self.cumulative_cost_usd),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class BudgetAggregate:
    provider: str
    operation: str
    stage: str
    calls: int
    usage_units: float
    usage_label: str
    total_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "operation": self.operation,
            "stage": self.stage,
            "calls": self.calls,
            "usage_units": round(self.usage_units, 6),
            "usage_label": self.usage_label,
            "total_cost_usd": _round_money(self.total_cost_usd),
        }


class RunBudgetTracker:
    def __init__(
        self,
        *,
        run_slug: str,
        events_path: str | Path,
        summary_path: str | Path,
        pricing: ScrapePricing | None = None,
    ) -> None:
        self.run_slug = run_slug
        self.events_path = Path(events_path)
        self.summary_path = Path(summary_path)
        self.pricing = pricing or ScrapePricing()
        self._events: list[BudgetEvent] = []
        self._cumulative_cost_usd = 0.0
        self._tracker_started_at = _utc_timestamp()
        self._summary_written_at = ""
        self._load_existing_events()

    def _load_existing_events(self) -> None:
        if not self.events_path.exists():
            return
        for raw_line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                continue
            event = BudgetEvent(
                recorded_at=str(payload.get("recorded_at") or ""),
                provider=str(payload.get("provider") or ""),
                stage=str(payload.get("stage") or ""),
                operation=str(payload.get("operation") or ""),
                calls=int(payload.get("calls") or 0),
                usage_units=float(payload.get("usage_units") or 0.0),
                usage_label=str(payload.get("usage_label") or ""),
                unit_cost_usd=float(payload.get("unit_cost_usd") or 0.0),
                total_cost_usd=float(payload.get("total_cost_usd") or 0.0),
                cumulative_cost_usd=float(payload.get("cumulative_cost_usd") or 0.0),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            )
            self._events.append(event)
        if self._events:
            self._cumulative_cost_usd = self._events[-1].cumulative_cost_usd

    def record_serper_search(
        self,
        *,
        stage: str,
        query: str,
        search_type: str = "search",
        num: int | None = None,
    ) -> BudgetEvent:
        unit_cost = (
            self.pricing.serper_news_usd
            if search_type == "news"
            else self.pricing.serper_search_usd
        )
        return self._record(
            provider="serper",
            stage=stage,
            operation=f"serper_{search_type}",
            calls=1,
            usage_units=1.0,
            usage_label="searches",
            unit_cost_usd=unit_cost,
            metadata={
                "query": query,
                "num": num or 0,
            },
        )

    def record_harvest_company_get(self, *, stage: str, company_url: str) -> BudgetEvent:
        return self._record(
            provider="harvest",
            stage=stage,
            operation="harvest_company_get",
            calls=1,
            usage_units=1.0,
            usage_label="requests",
            unit_cost_usd=self.pricing.harvest_company_get_usd,
            metadata={"company_url": company_url},
        )

    def record_harvest_company_posts(
        self,
        *,
        stage: str,
        company_url: str,
        page: int,
    ) -> BudgetEvent:
        return self._record(
            provider="harvest",
            stage=stage,
            operation="harvest_company_posts",
            calls=1,
            usage_units=1.0,
            usage_label="requests",
            unit_cost_usd=self.pricing.harvest_company_posts_usd,
            metadata={"company_url": company_url, "page": page},
        )

    def record_harvest_profile_get(
        self,
        *,
        stage: str,
        linkedin_url: str,
        include_email: bool,
        use_main_profile: bool,
    ) -> BudgetEvent:
        if include_email:
            unit_cost = self.pricing.harvest_profile_with_email_usd
            operation = "harvest_profile_get_with_email"
        elif use_main_profile:
            unit_cost = self.pricing.harvest_main_profile_usd
            operation = "harvest_main_profile_get"
        else:
            unit_cost = self.pricing.harvest_full_profile_usd
            operation = "harvest_full_profile_get"
        return self._record(
            provider="harvest",
            stage=stage,
            operation=operation,
            calls=1,
            usage_units=1.0,
            usage_label="requests",
            unit_cost_usd=unit_cost,
            metadata={
                "linkedin_url": linkedin_url,
                "include_email": include_email,
                "use_main_profile": use_main_profile,
            },
        )

    def record_harvest_profile_posts(
        self,
        *,
        stage: str,
        linkedin_url: str,
        page: int,
    ) -> BudgetEvent:
        return self._record(
            provider="harvest",
            stage=stage,
            operation="harvest_profile_posts",
            calls=1,
            usage_units=1.0,
            usage_label="requests",
            unit_cost_usd=self.pricing.harvest_profile_posts_usd,
            metadata={"linkedin_url": linkedin_url, "page": page},
        )

    def record_minimax_company_reasoning(
        self,
        *,
        stage: str,
        company_name: str,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> BudgetEvent:
        return self._record(
            provider="minimax",
            stage=stage,
            operation="minimax_company_reasoning",
            calls=1,
            usage_units=1.0,
            usage_label="calls",
            unit_cost_usd=self.pricing.minimax_company_reasoning_usd,
            metadata={
                "company_name": company_name,
                "model": model,
                "prompt_tokens": prompt_tokens or 0,
                "completion_tokens": completion_tokens or 0,
            },
        )

    def record_brightdata_crawl(
        self,
        *,
        stage: str,
        urls: list[str],
        dataset_id: str,
    ) -> BudgetEvent:
        return self._record(
            provider="brightdata",
            stage=stage,
            operation="brightdata_crawl_records",
            calls=1,
            usage_units=float(len(urls)),
            usage_label="records",
            unit_cost_usd=self.pricing.brightdata_record_usd,
            metadata={
                "dataset_id": dataset_id,
                "urls": urls,
            },
        )

    def record_firecrawl_map(
        self,
        *,
        stage: str,
        company_domain: str,
    ) -> BudgetEvent:
        credits = self.pricing.firecrawl_map_credits_per_request
        return self._record(
            provider="firecrawl",
            stage=stage,
            operation="firecrawl_map",
            calls=1,
            usage_units=credits,
            usage_label="credits",
            unit_cost_usd=self.pricing.firecrawl_credit_usd,
            metadata={"company_domain": company_domain},
        )

    def record_firecrawl_scrape(
        self,
        *,
        stage: str,
        url: str,
    ) -> BudgetEvent:
        credits = self.pricing.firecrawl_scrape_credits_per_page
        return self._record(
            provider="firecrawl",
            stage=stage,
            operation="firecrawl_scrape_page",
            calls=1,
            usage_units=credits,
            usage_label="credits",
            unit_cost_usd=self.pricing.firecrawl_credit_usd,
            metadata={"url": url},
        )

    def _record(
        self,
        *,
        provider: str,
        stage: str,
        operation: str,
        calls: int,
        usage_units: float,
        usage_label: str,
        unit_cost_usd: float,
        metadata: dict[str, Any] | None = None,
    ) -> BudgetEvent:
        total_cost = usage_units * unit_cost_usd
        self._cumulative_cost_usd += total_cost
        event = BudgetEvent(
            recorded_at=_utc_timestamp(),
            provider=provider,
            stage=stage,
            operation=operation,
            calls=calls,
            usage_units=usage_units,
            usage_label=usage_label,
            unit_cost_usd=unit_cost_usd,
            total_cost_usd=total_cost,
            cumulative_cost_usd=self._cumulative_cost_usd,
            metadata=metadata or {},
        )
        self._events.append(event)
        append_jsonl(
            self.events_path,
            {
                "run_slug": self.run_slug,
                **event.to_dict(),
            },
        )
        return event

    def summary(self) -> dict[str, Any]:
        operation_totals: dict[tuple[str, str, str, str], BudgetAggregate] = {}
        provider_totals: dict[str, dict[str, float | int]] = {}
        operation_windows: dict[tuple[str, str, str, str], tuple[str, str]] = {}
        stage_totals: dict[tuple[str, str, str], BudgetAggregate] = {}
        stage_windows: dict[tuple[str, str, str], tuple[str, str]] = {}
        provider_windows: dict[str, tuple[str, str]] = {}

        for event in self._events:
            operation_key = (
                event.provider,
                event.stage,
                event.operation,
                event.usage_label,
            )
            aggregate = operation_totals.get(operation_key)
            if aggregate is None:
                operation_totals[operation_key] = BudgetAggregate(
                    provider=event.provider,
                    operation=event.operation,
                    stage=event.stage,
                    calls=event.calls,
                    usage_units=event.usage_units,
                    usage_label=event.usage_label,
                    total_cost_usd=event.total_cost_usd,
                )
            else:
                operation_totals[operation_key] = BudgetAggregate(
                    provider=aggregate.provider,
                    operation=aggregate.operation,
                    stage=aggregate.stage,
                    calls=aggregate.calls + event.calls,
                    usage_units=aggregate.usage_units + event.usage_units,
                    usage_label=aggregate.usage_label,
                    total_cost_usd=aggregate.total_cost_usd + event.total_cost_usd,
                )

            existing_operation_window = operation_windows.get(operation_key)
            if existing_operation_window is None:
                operation_windows[operation_key] = (event.recorded_at, event.recorded_at)
            else:
                operation_windows[operation_key] = (
                    min(existing_operation_window[0], event.recorded_at),
                    max(existing_operation_window[1], event.recorded_at),
                )

            stage_key = (event.provider, event.stage, event.usage_label)
            stage_aggregate = stage_totals.get(stage_key)
            if stage_aggregate is None:
                stage_totals[stage_key] = BudgetAggregate(
                    provider=event.provider,
                    operation="",
                    stage=event.stage,
                    calls=event.calls,
                    usage_units=event.usage_units,
                    usage_label=event.usage_label,
                    total_cost_usd=event.total_cost_usd,
                )
            else:
                stage_totals[stage_key] = BudgetAggregate(
                    provider=stage_aggregate.provider,
                    operation="",
                    stage=stage_aggregate.stage,
                    calls=stage_aggregate.calls + event.calls,
                    usage_units=stage_aggregate.usage_units + event.usage_units,
                    usage_label=stage_aggregate.usage_label,
                    total_cost_usd=stage_aggregate.total_cost_usd + event.total_cost_usd,
                )
            existing_stage_window = stage_windows.get(stage_key)
            if existing_stage_window is None:
                stage_windows[stage_key] = (event.recorded_at, event.recorded_at)
            else:
                stage_windows[stage_key] = (
                    min(existing_stage_window[0], event.recorded_at),
                    max(existing_stage_window[1], event.recorded_at),
                )

            provider_payload = provider_totals.setdefault(
                event.provider,
                {"calls": 0, "total_cost_usd": 0.0},
            )
            provider_payload["calls"] = int(provider_payload["calls"]) + event.calls
            provider_payload["total_cost_usd"] = float(
                provider_payload["total_cost_usd"]
            ) + event.total_cost_usd
            existing_provider_window = provider_windows.get(event.provider)
            if existing_provider_window is None:
                provider_windows[event.provider] = (event.recorded_at, event.recorded_at)
            else:
                provider_windows[event.provider] = (
                    min(existing_provider_window[0], event.recorded_at),
                    max(existing_provider_window[1], event.recorded_at),
                )

        first_event_at = min((event.recorded_at for event in self._events), default="")
        last_event_at = max((event.recorded_at for event in self._events), default="")
        event_runtime_seconds = _duration_seconds(first_event_at, last_event_at)
        summary_process_runtime_seconds = _duration_seconds(
            self._tracker_started_at,
            self._summary_written_at or self._tracker_started_at,
        )

        def _timing_payload(
            *,
            started_at: str,
            completed_at: str,
            calls: int,
            total_cost_usd: float,
        ) -> dict[str, Any]:
            elapsed_seconds = _duration_seconds(started_at, completed_at)
            return {
                "started_at": started_at,
                "completed_at": completed_at,
                "elapsed_seconds": round(elapsed_seconds or 0.0, 3),
                "elapsed_minutes": round((elapsed_seconds or 0.0) / 60.0, 3),
                "elapsed_hms": _format_duration(elapsed_seconds),
                "calls_per_minute": _rate_per_minute(calls, elapsed_seconds),
                "cost_per_minute_usd": _rate_per_minute(total_cost_usd, elapsed_seconds),
            }

        return {
            "run_slug": self.run_slug,
            "pricing": self.pricing.to_dict(),
            "total_cost_usd": _round_money(self._cumulative_cost_usd),
            "event_count": len(self._events),
            "events_path": str(self.events_path),
            "summary_path": str(self.summary_path),
            "api_audit_log_path": os.environ.get("LISTBUILD_API_AUDIT_LOG", "").strip(),
            "timing": {
                "summary_process_started_at": self._tracker_started_at,
                "summary_written_at": self._summary_written_at,
                "summary_process_duration_seconds": round(summary_process_runtime_seconds or 0.0, 3),
                "summary_process_duration_minutes": round((summary_process_runtime_seconds or 0.0) / 60.0, 3),
                "summary_process_duration_hms": _format_duration(summary_process_runtime_seconds),
                "workflow_started_at": first_event_at,
                "workflow_completed_at": last_event_at,
                "first_event_at": first_event_at,
                "last_event_at": last_event_at,
                "event_duration_seconds": round(event_runtime_seconds or 0.0, 3),
                "event_duration_minutes": round((event_runtime_seconds or 0.0) / 60.0, 3),
                "event_duration_hms": _format_duration(event_runtime_seconds),
            },
            "provider_totals": {
                provider: {
                    "calls": values["calls"],
                    "total_cost_usd": _round_money(float(values["total_cost_usd"])),
                    **_timing_payload(
                        started_at=provider_windows.get(provider, ("", ""))[0],
                        completed_at=provider_windows.get(provider, ("", ""))[1],
                        calls=int(values["calls"]),
                        total_cost_usd=float(values["total_cost_usd"]),
                    ),
                }
                for provider, values in sorted(provider_totals.items())
            },
            "stage_totals": [
                {
                    "provider": aggregate.provider,
                    "stage": aggregate.stage,
                    "calls": aggregate.calls,
                    "usage_units": round(aggregate.usage_units, 6),
                    "usage_label": aggregate.usage_label,
                    "total_cost_usd": _round_money(aggregate.total_cost_usd),
                    **_timing_payload(
                        started_at=stage_windows.get(
                            (aggregate.provider, aggregate.stage, aggregate.usage_label),
                            ("", ""),
                        )[0],
                        completed_at=stage_windows.get(
                            (aggregate.provider, aggregate.stage, aggregate.usage_label),
                            ("", ""),
                        )[1],
                        calls=aggregate.calls,
                        total_cost_usd=aggregate.total_cost_usd,
                    ),
                }
                for aggregate in sorted(
                    stage_totals.values(),
                    key=lambda item: (item.provider, item.stage, item.usage_label),
                )
            ],
            "operation_totals": [
                {
                    **aggregate.to_dict(),
                    **_timing_payload(
                        started_at=operation_windows.get(
                            (aggregate.provider, aggregate.stage, aggregate.operation, aggregate.usage_label),
                            ("", ""),
                        )[0],
                        completed_at=operation_windows.get(
                            (aggregate.provider, aggregate.stage, aggregate.operation, aggregate.usage_label),
                            ("", ""),
                        )[1],
                        calls=aggregate.calls,
                        total_cost_usd=aggregate.total_cost_usd,
                    ),
                }
                for aggregate in sorted(
                    operation_totals.values(),
                    key=lambda item: (item.provider, item.stage, item.operation),
                )
            ],
        }

    def write_summary(self) -> dict[str, Any]:
        self._summary_written_at = _utc_timestamp()
        summary = self.summary()
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return summary
