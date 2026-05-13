#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from collections import OrderedDict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from listbuild.config import ProviderSettings, load_local_env
from listbuild.normalize import normalize_url
from listbuild.providers import HarvestClient, SerperClient


DEFAULT_KEYWORDS: tuple[str, ...] = ("MSP", "MSSP")
DEFAULT_HEADCOUNT_FILTERS: tuple[str, ...] = ("51-200", "201-500")
DEFAULT_QUERY_TEMPLATE = 'site:linkedin.com/company/ "{keyword}" "{headcount_filter}"'


@dataclass(frozen=True, slots=True)
class QuerySpec:
    keyword: str
    headcount_filter: str
    query_template: str

    @property
    def query(self) -> str:
        return self.query_template.format(
            keyword=self.keyword,
            headcount_filter=self.headcount_filter,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect LinkedIn company URLs from Serper for a configurable keyword/headcount "
            "grid, optionally hydrate companies with Harvest, and export reusable CSV/JSONL "
            "artifacts for campaign building."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="/Users/mortonstreet/alertica/alertica-tiers",
        help="Directory for CSV and raw JSONL archives.",
    )
    parser.add_argument(
        "--base-name",
        default="alertica_msp_mssp_51-200_201-500",
        help="Base name prefix for generated artifacts.",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords",
        help=(
            "Keyword to inject into the search template. Repeatable. "
            f"Defaults to {', '.join(DEFAULT_KEYWORDS)} when omitted."
        ),
    )
    parser.add_argument(
        "--headcount-filter",
        action="append",
        dest="headcount_filters",
        help=(
            "Headcount filter to inject into the search template. Repeatable. "
            f"Defaults to {', '.join(DEFAULT_HEADCOUNT_FILTERS)} when omitted."
        ),
    )
    parser.add_argument(
        "--query-template",
        default=DEFAULT_QUERY_TEMPLATE,
        help=(
            "Python format string for each search query. Available placeholders: "
            "{keyword}, {headcount_filter}."
        ),
    )
    parser.add_argument(
        "--serper-only",
        action="store_true",
        help="Skip Harvest hydration and emit Serper-derived company rows only.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=100,
        help="Pages per query to request from Serper.",
    )
    parser.add_argument(
        "--num",
        type=int,
        default=10,
        help="Results per Serper page.",
    )
    parser.add_argument(
        "--gl",
        default=None,
        help="Optional Serper country bias, for example 'us'.",
    )
    parser.add_argument(
        "--hl",
        default=None,
        help="Optional Serper language hint, for example 'en'.",
    )
    parser.add_argument(
        "--harvest-concurrency",
        type=int,
        default=5,
        help="Concurrent LinkedIn company hydration requests.",
    )
    parser.add_argument(
        "--serper-concurrency",
        type=int,
        default=8,
        help="Concurrent Serper page requests.",
    )
    return parser.parse_args()


def build_query_specs(args: argparse.Namespace) -> list[QuerySpec]:
    keywords = tuple(args.keywords or DEFAULT_KEYWORDS)
    headcount_filters = tuple(args.headcount_filters or DEFAULT_HEADCOUNT_FILTERS)
    return [
        QuerySpec(
            keyword=keyword.strip(),
            headcount_filter=headcount_filter.strip(),
            query_template=args.query_template,
        )
        for keyword in keywords
        for headcount_filter in headcount_filters
        if keyword.strip() and headcount_filter.strip()
    ]


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    return int(raw_value)


def optional_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    return float(raw_value)


def load_serper_settings() -> ProviderSettings:
    load_local_env()
    direct_key = os.environ.get("SERPER_API_KEY", "").strip()
    indexed_keys = sorted(
        (key, value.strip())
        for key, value in os.environ.items()
        if key.startswith("SERPER_API_KEY") and key != "SERPER_API_KEY" and value.strip()
    )
    api_keys: list[str] = []
    if direct_key:
        api_keys.append(direct_key)
    api_keys.extend(value for _, value in indexed_keys)
    if not api_keys:
        raise RuntimeError("At least one SERPER_API_KEY value is required")
    timeout_seconds = optional_float("LISTBUILD_TIMEOUT_SECONDS", 30.0)
    return ProviderSettings(
        name="serper",
        base_url="https://google.serper.dev",
        api_keys=tuple(api_keys),
        timeout_seconds=optional_float("SERPER_TIMEOUT_SECONDS", timeout_seconds),
        requests_per_second=optional_float("SERPER_QPS", 50.0),
        burst=optional_int("SERPER_BURST", 50),
        max_connections=optional_int("SERPER_MAX_CONNECTIONS", 100),
    )


def load_harvest_settings() -> ProviderSettings:
    load_local_env()
    timeout_seconds = optional_float("LISTBUILD_TIMEOUT_SECONDS", 30.0)
    return ProviderSettings(
        name="harvest",
        base_url="https://api.harvest-api.com",
        api_keys=(require_env("HARVEST_API_KEY"),),
        timeout_seconds=optional_float("HARVEST_TIMEOUT_SECONDS", timeout_seconds),
        requests_per_second=optional_float("HARVEST_QPS", 0.0),
        burst=optional_int("HARVEST_BURST", 5),
        max_connections=optional_int("HARVEST_MAX_CONNECTIONS", 5),
    )


def normalize_linkedin_company_url(raw_url: str) -> str:
    normalized = normalize_url(raw_url)
    if normalized is None:
        return raw_url.strip()
    parsed = urlsplit(normalized.normalized_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "company":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    slug = parts[1].strip().lower()
    canonical_path = f"/company/{slug}"
    return urlunsplit(("https", "www.linkedin.com", canonical_path, "", ""))


def headcount_range_text(company_element: dict[str, Any]) -> str:
    employee_count_range = company_element.get("employeeCountRange")
    if not isinstance(employee_count_range, dict):
        return ""
    start = employee_count_range.get("start")
    end = employee_count_range.get("end")
    if start is None and end is None:
        return ""
    if start is None:
        return f"up to {end}"
    if end is None:
        return f"{start}+"
    return f"{start}-{end}"


def company_domain_from_element(company_element: dict[str, Any]) -> str:
    website = str(company_element.get("website") or "").strip()
    if not website:
        return ""
    normalized = normalize_url(website)
    if normalized is None:
        return website.lower().removeprefix("www.")
    return normalized.host


def slug_to_company_name(company_url: str) -> str:
    parsed = urlsplit(company_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    slug = parts[1]
    if not slug:
        return ""
    words = [token for token in re.split(r"[-_]+", slug) if token]
    return " ".join(word.upper() if word.isupper() else word.capitalize() for word in words)


def company_name_from_serper_title(title: str, company_url: str) -> str:
    cleaned = re.sub(r"\s*\|\s*LinkedIn.*$", "", title.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*(linkedin|领英)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(?::\s*(overview|employees|jobs|location|about|posts|people))$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"^LinkedIn:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or slug_to_company_name(company_url)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_linkedin_company_hits(
    *,
    payload: dict[str, Any],
    query_spec: QuerySpec,
    page: int,
) -> list[dict[str, Any]]:
    organic = payload.get("organic")
    if not isinstance(organic, list):
        return []

    rows: list[dict[str, Any]] = []
    for default_rank, item in enumerate(organic, start=1):
        if not isinstance(item, dict):
            continue
        candidate = item.get("link") or item.get("url")
        if not isinstance(candidate, str):
            continue
        if "linkedin.com/company/" not in candidate.lower():
            continue
        company_url = normalize_linkedin_company_url(candidate)
        serper_title = str(item.get("title") or "").strip()
        rows.append(
            {
                "query_keyword": query_spec.keyword,
                "query_headcount_filter": query_spec.headcount_filter,
                "query": query_spec.query,
                "page": page,
                "rank": item.get("position") or default_rank,
                "company_linkedin_url": company_url,
                "company_name": company_name_from_serper_title(serper_title, company_url),
                "serper_title": serper_title,
                "serper_snippet": str(item.get("snippet") or "").strip(),
                "serper_result_json": json.dumps(item, ensure_ascii=False),
            }
        )
    return rows


async def collect_serper_hits(
    *,
    serper: SerperClient,
    query_specs: list[QuerySpec],
    pages: int,
    num: int,
    gl: str | None,
    hl: str | None,
    serper_concurrency: int,
    serper_archive_path: Path,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(serper_concurrency)
    results: list[dict[str, Any]] = []
    results_lock = asyncio.Lock()

    async def run_page(query_spec: QuerySpec, page: int) -> None:
        async with semaphore:
            response = await serper.search(
                query_spec.query,
                gl=gl,
                hl=hl,
                num=num,
                page=page,
                autocorrect=False,
            )
        payload = {
            "provider": response.provider,
            "status_code": response.status_code,
            "rate_limit": asdict(response.rate_limit),
            "query_keyword": query_spec.keyword,
            "query_headcount_filter": query_spec.headcount_filter,
            "query": query_spec.query,
            "page": page,
            "data": response.data,
        }
        append_jsonl(serper_archive_path, payload)
        page_hits = extract_linkedin_company_hits(
            payload=response.data if isinstance(response.data, dict) else {},
            query_spec=query_spec,
            page=page,
        )
        async with results_lock:
            results.extend(page_hits)

    await asyncio.gather(
        *(
            run_page(query_spec, page)
            for query_spec in query_specs
            for page in range(1, pages + 1)
        )
    )
    return sorted(
        results,
        key=lambda item: (
            item["query_keyword"],
            item["query_headcount_filter"],
            int(item["page"]),
            int(item["rank"]),
        ),
    )


def group_hits_by_company(hits: list[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for hit in hits:
        company_url = hit["company_linkedin_url"]
        grouped.setdefault(company_url, []).append(hit)
    return grouped


async def hydrate_companies(
    *,
    harvest: HarvestClient,
    grouped_hits: OrderedDict[str, list[dict[str, Any]]],
    harvest_concurrency: int,
    harvest_archive_path: Path,
) -> OrderedDict[str, dict[str, Any]]:
    semaphore = asyncio.Semaphore(harvest_concurrency)
    hydrated: OrderedDict[str, dict[str, Any]] = OrderedDict()
    hydrated_lock = asyncio.Lock()

    async def run_company(company_url: str, hits: list[dict[str, Any]]) -> None:
        async with semaphore:
            response = await harvest.get_company(url=company_url)
        record = {
            "provider": response.provider,
            "status_code": response.status_code,
            "rate_limit": asdict(response.rate_limit),
            "company_linkedin_url": company_url,
            "matched_queries": sorted({hit["query"] for hit in hits}),
            "data": response.data,
        }
        append_jsonl(harvest_archive_path, record)
        async with hydrated_lock:
            hydrated[company_url] = record

    await asyncio.gather(
        *(run_company(company_url, hits) for company_url, hits in grouped_hits.items())
    )
    return hydrated


def best_company_name(
    hits: list[dict[str, Any]],
    company_url: str,
    element: dict[str, Any] | None = None,
) -> str:
    if isinstance(element, dict):
        harvest_name = str(element.get("name") or "").strip()
        if harvest_name:
            return harvest_name
    names = [
        str(hit.get("company_name") or "").strip()
        for hit in hits
        if str(hit.get("company_name") or "").strip()
    ]
    if names:
        names.sort(key=lambda value: (-len(value), value.lower()))
        return names[0]
    return slug_to_company_name(company_url)


def hit_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    for hit in hits:
        row = dict(hit)
        row["first_seen_at"] = seen_at
        rows.append(row)
    return rows


def final_rows(
    *,
    grouped_hits: OrderedDict[str, list[dict[str, Any]]],
    hydrated_records: OrderedDict[str, dict[str, Any]],
    serper_only: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    for company_url, hits in grouped_hits.items():
        hydrated = hydrated_records.get(company_url, {})
        payload = hydrated.get("data")
        if not isinstance(payload, dict):
            payload = {}
        element = payload.get("element")
        if not isinstance(element, dict):
            element = {}

        company_name = best_company_name(hits, company_url, element)
        company_domain = company_domain_from_element(element)
        headcount_exact_raw = element.get("employeeCount")
        headcount_exact = "" if headcount_exact_raw in (None, "") else str(headcount_exact_raw)
        headcount_range = headcount_range_text(element)
        company_size = headcount_range or headcount_exact
        top_hit = hits[0]

        rows.append(
            {
                "company_name": company_name,
                "company_linkedin_url": company_url,
                "company_domain": company_domain,
                "company_size": company_size,
                "headcount_exact": headcount_exact,
                "headcount_range": headcount_range,
                "target_headcount_filters": " | ".join(
                    sorted({hit["query_headcount_filter"] for hit in hits})
                ),
                "matched_queries": " | ".join(sorted({hit["query"] for hit in hits})),
                "matched_keywords": " | ".join(sorted({hit["query_keyword"] for hit in hits})),
                "best_serper_title": str(top_hit.get("serper_title") or "").strip(),
                "best_serper_snippet": str(top_hit.get("serper_snippet") or "").strip(),
                "serper_hit_count": str(len(hits)),
                "first_seen_at": seen_at,
                "serper_hits_json": json.dumps(hits, ensure_ascii=False),
                "harvest_json": "" if serper_only else json.dumps(payload, ensure_ascii=False),
            }
        )
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    base_name = args.base_name
    company_csv_path = output_dir / f"{base_name}_companies.csv"
    hit_csv_path = output_dir / f"{base_name}_serper_hits.csv"
    serper_archive_path = output_dir / f"{base_name}_serper_pages.jsonl"
    harvest_archive_path = output_dir / f"{base_name}_harvest_companies.jsonl"
    hit_archive_path = output_dir / f"{base_name}_serper_company_hits.jsonl"

    cleanup_paths = [
        company_csv_path,
        hit_csv_path,
        serper_archive_path,
        hit_archive_path,
    ]
    if not args.serper_only:
        cleanup_paths.append(harvest_archive_path)

    for archive_path in cleanup_paths:
        if archive_path.exists():
            archive_path.unlink()

    query_specs = build_query_specs(args)
    if not query_specs:
        raise RuntimeError("At least one query spec is required")

    serper_settings = load_serper_settings()
    async with SerperClient(serper_settings) as serper:
        hits = await collect_serper_hits(
            serper=serper,
            query_specs=query_specs,
            pages=args.pages,
            num=args.num,
            gl=args.gl,
            hl=args.hl,
            serper_concurrency=args.serper_concurrency,
            serper_archive_path=serper_archive_path,
        )

    for hit in hits:
        append_jsonl(hit_archive_path, hit)

    grouped_hits = group_hits_by_company(hits)
    hydrated_records: OrderedDict[str, dict[str, Any]] = OrderedDict()
    if not args.serper_only:
        harvest_settings = load_harvest_settings()
        async with HarvestClient(harvest_settings) as harvest:
            hydrated_records = await hydrate_companies(
                harvest=harvest,
                grouped_hits=grouped_hits,
                harvest_concurrency=args.harvest_concurrency,
                harvest_archive_path=harvest_archive_path,
            )

    write_csv(
        hit_rows(hits),
        hit_csv_path,
        fieldnames=[
            "company_name",
            "company_linkedin_url",
            "query_keyword",
            "query_headcount_filter",
            "query",
            "page",
            "rank",
            "serper_title",
            "serper_snippet",
            "serper_result_json",
            "first_seen_at",
        ],
    )

    rows = final_rows(
        grouped_hits=grouped_hits,
        hydrated_records=hydrated_records,
        serper_only=args.serper_only,
    )
    write_csv(
        rows,
        company_csv_path,
        fieldnames=[
            "company_name",
            "company_linkedin_url",
            "company_domain",
            "company_size",
            "headcount_exact",
            "headcount_range",
            "target_headcount_filters",
            "matched_queries",
            "matched_keywords",
            "best_serper_title",
            "best_serper_snippet",
            "serper_hit_count",
            "first_seen_at",
            "serper_hits_json",
            "harvest_json",
        ],
    )

    summary = {
        "company_csv": str(company_csv_path),
        "serper_hits_csv": str(hit_csv_path),
        "serper_archive_jsonl": str(serper_archive_path),
        "harvest_archive_jsonl": "" if args.serper_only else str(harvest_archive_path),
        "serper_hit_archive_jsonl": str(hit_archive_path),
        "query_count": len(query_specs),
        "queries": [query_spec.query for query_spec in query_specs],
        "pages_per_query": args.pages,
        "total_serper_requests": len(query_specs) * args.pages,
        "linkedin_hit_count": len(hits),
        "unique_company_count": len(rows),
        "serper_only": args.serper_only,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> int:
    args = parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
