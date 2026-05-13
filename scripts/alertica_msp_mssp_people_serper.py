#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
from dataclasses import asdict
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from listbuild.config import ProviderSettings, load_local_env
from listbuild.jsonl import append_jsonl
from listbuild.normalize import normalize_url
from listbuild.providers import SerperClient


DEFAULT_COMPANIES_CSV = "/Users/mortonstreet/alertica/alertica-tiers/alertica_msp_mssp_51-200_201-500_companies.csv"
DEFAULT_OUTPUT_DIR = "/Users/mortonstreet/alertica/alertica-tiers"
DEFAULT_BASE_NAME = "alertica_msp_mssp_51-200_201-500_people_serper"
SERPER_COST_PER_SEARCH_USD = 0.003


@dataclass(frozen=True, slots=True)
class RoleSegment:
    segment_name: str
    titles: tuple[str, ...]


DEFAULT_ROLE_SEGMENTS: tuple[RoleSegment, ...] = (
    RoleSegment(
        segment_name="leadership",
        titles=("CEO", "President", "Owner", "COO", "CFO", "CMO", "Founder", "Co-founder", "Co-Founder"),
    ),
    RoleSegment(
        segment_name="it_ops",
        titles=("IT Manager", "System Administrator", "IT Operations", "Infrastructure Engineer"),
    ),
    RoleSegment(
        segment_name="security",
        titles=("Security Manager", "Security Analyst", "Security Engineer"),
    ),
    RoleSegment(
        segment_name="commercial",
        titles=("Account Manager", "Business Development"),
    ),
    RoleSegment(
        segment_name="admin",
        titles=("Administrative Assistant",),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a Serper-only LinkedIn people discovery pass for the Alertica MSP/MSSP "
            "company list and export raw candidates for later Harvest validation."
        )
    )
    parser.add_argument(
        "--companies-csv",
        default=DEFAULT_COMPANIES_CSV,
        help="Source MSP/MSSP companies CSV.",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for output artifacts.",
    )
    parser.add_argument(
        "--base-name",
        default=DEFAULT_BASE_NAME,
        help="Base filename prefix for generated artifacts.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum Serper pages to request per company/role-segment query.",
    )
    parser.add_argument(
        "--num",
        type=int,
        default=10,
        help="Results per Serper page.",
    )
    parser.add_argument(
        "--serper-concurrency",
        type=int,
        default=8,
        help="Concurrent company discovery tasks.",
    )
    parser.add_argument(
        "--candidate-pool-multiplier",
        type=float,
        default=2.0,
        help=(
            "Serper-only target pool multiplier relative to the later validated people cap. "
            "For example, 2.0 collects up to 10 raw candidates for <=200 companies and 20 "
            "for >200 companies."
        ),
    )
    parser.add_argument(
        "--max-people-per-company-under-200",
        type=int,
        default=5,
        help="Later validated people cap for companies at or under 200 headcount.",
    )
    parser.add_argument(
        "--max-people-per-company-over-200",
        type=int,
        default=10,
        help="Later validated people cap for companies above 200 headcount.",
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
    return parser.parse_args()


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


def normalize_linkedin_profile_url(raw_url: str) -> str:
    normalized = normalize_url(raw_url)
    if normalized is None:
        return raw_url.strip()
    parsed = urlsplit(normalized.normalized_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "in":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    slug = parts[1].strip().lower()
    canonical_path = f"/in/{slug}"
    return urlunsplit(("https", "www.linkedin.com", canonical_path, "", ""))


def normalize_domain(raw_value: str) -> str:
    normalized = normalize_url(raw_value)
    if normalized is None:
        return raw_value.strip().lower().removeprefix("www.")
    return normalized.host


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def normalize_company_name_key(value: str) -> str:
    lowered = clean_text(value).casefold()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(collapsed.split())


def company_key(row: dict[str, str]) -> str:
    company_domain = normalize_domain(str(row.get("company_domain") or ""))
    if company_domain:
        return f"domain:{company_domain}"
    linkedin_url = normalize_linkedin_company_url(
        str(row.get("company_linkedin_url") or row.get("linkedin_company_url") or "")
    )
    if linkedin_url:
        return f"linkedin:{linkedin_url}"
    company_name = normalize_company_name_key(str(row.get("company_name") or ""))
    return f"name:{company_name}"


def row_quality_score(row: dict[str, str]) -> tuple[int, int]:
    important_fields = (
        "company_name",
        "company_domain",
        "company_linkedin_url",
        "headcount_exact",
        "headcount_range",
        "company_size",
        "company_description",
        "company_offer",
        "company_icp",
        "company_painpoint",
        "company_signals",
        "company_signal_sources",
    )
    filled = sum(1 for field in important_fields if clean_text(row.get(field) or ""))
    return (filled, len(clean_text(json.dumps(row, ensure_ascii=False))))


def prefer_company_row(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    return right if row_quality_score(right) > row_quality_score(left) else left


def range_bounds(raw_value: str) -> tuple[int | None, int | None]:
    stripped = raw_value.strip().lower()
    if not stripped:
        return None, None
    digits = [int(value) for value in re.findall(r"\d+", stripped)]
    if not digits:
        return None, None
    if stripped.endswith("+"):
        return digits[0], None
    if len(digits) >= 2:
        return digits[0], digits[1]
    return digits[0], digits[0]


def company_is_over_200(company: dict[str, str]) -> bool | None:
    for key in ("headcount_range", "company_headcount_range", "company_size"):
        start, end = range_bounds(clean_text(company.get(key) or ""))
        if start is None and end is None:
            continue
        if start is not None and start > 200:
            return True
        if end is not None:
            return end > 200
    for key in ("headcount_exact", "company_headcount_exact"):
        raw_value = clean_text(company.get(key) or "")
        if not raw_value:
            continue
        try:
            return int(raw_value) > 200
        except ValueError:
            continue
    return None


def target_people_cap(company: dict[str, str], args: argparse.Namespace) -> int:
    return (
        args.max_people_per_company_over_200
        if company_is_over_200(company) is True
        else args.max_people_per_company_under_200
    )


def candidate_pool_target(company: dict[str, str], args: argparse.Namespace) -> int:
    base_target = target_people_cap(company, args)
    return max(base_target, int(math.ceil(base_target * args.candidate_pool_multiplier)))


def dedupe_companies(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in rows:
        key = company_key(row)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = dict(row)
            order.append(key)
        else:
            deduped[key] = prefer_company_row(existing, row)
    return [deduped[key] for key in order]


def quote_term(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


def role_query(company: dict[str, str], role_segment: RoleSegment) -> str:
    company_name = clean_text(company.get("company_name") or "")
    company_domain = clean_text(company.get("company_domain") or "")
    titles = " OR ".join(quote_term(title) for title in role_segment.titles if clean_text(title))
    company_clause = quote_term(company_name)
    if company_domain:
        company_clause = f"({quote_term(company_name)} OR {quote_term(company_domain)})"
    return f"site:linkedin.com/in/ {company_clause} ({titles})"


def guess_name_from_title(title: str) -> str:
    cleaned = re.sub(r"\s*\|\s*LinkedIn.*$", "", clean_text(title), flags=re.IGNORECASE)
    parts = [part.strip() for part in cleaned.split(" - ") if part.strip()]
    if not parts:
        return ""
    return parts[0]


def guess_role_from_title(title: str) -> str:
    cleaned = re.sub(r"\s*\|\s*LinkedIn.*$", "", clean_text(title), flags=re.IGNORECASE)
    parts = [part.strip() for part in cleaned.split(" - ") if part.strip()]
    if len(parts) >= 2:
        return parts[1]
    return ""


def extract_linkedin_profile_hits(
    *,
    payload: dict[str, Any],
    company: dict[str, str],
    role_segment: RoleSegment,
    query: str,
    page: int,
) -> list[dict[str, Any]]:
    organic = payload.get("organic")
    if not isinstance(organic, list):
        return []

    hits: list[dict[str, Any]] = []
    for default_rank, item in enumerate(organic, start=1):
        if not isinstance(item, dict):
            continue
        candidate = item.get("link") or item.get("url")
        if not isinstance(candidate, str):
            continue
        if "linkedin.com/in/" not in candidate.lower():
            continue
        linkedin_url = normalize_linkedin_profile_url(candidate)
        serper_title = clean_text(item.get("title") or "")
        serper_snippet = clean_text(item.get("snippet") or "")
        hits.append(
            {
                "company_name": clean_text(company.get("company_name") or ""),
                "company_domain": clean_text(company.get("company_domain") or ""),
                "company_linkedin_url": normalize_linkedin_company_url(
                    clean_text(company.get("company_linkedin_url") or "")
                ),
                "headcount_exact": clean_text(company.get("headcount_exact") or ""),
                "headcount_range": clean_text(company.get("headcount_range") or ""),
                "company_size": clean_text(company.get("company_size") or ""),
                "role_segment": role_segment.segment_name,
                "requested_titles": list(role_segment.titles),
                "query": query,
                "page": page,
                "rank": int(item.get("position") or default_rank),
                "linkedin_profile_url": linkedin_url,
                "person_name_guess": guess_name_from_title(serper_title),
                "role_guess": guess_role_from_title(serper_title),
                "serper_title": serper_title,
                "serper_snippet": serper_snippet,
                "serper_result_json": json.dumps(item, ensure_ascii=False),
            }
        )
    return hits


def read_companies_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


async def collect_company_candidates(
    *,
    company: dict[str, str],
    role_segments: tuple[RoleSegment, ...],
    serper: SerperClient,
    args: argparse.Namespace,
    archive_lock: asyncio.Lock,
    serper_archive_path: Path,
) -> dict[str, Any]:
    discovered: dict[str, dict[str, Any]] = {}
    page_requests = 0
    empty_pages = 0
    cap = target_people_cap(company, args)
    pool_target = candidate_pool_target(company, args)

    for role_segment in role_segments:
        if len(discovered) >= pool_target:
            break
        query = role_query(company, role_segment)
        for page in range(1, args.max_pages + 1):
            if len(discovered) >= pool_target:
                break
            response = await serper.search(
                query,
                gl=args.gl,
                hl=args.hl,
                num=args.num,
                page=page,
                autocorrect=False,
            )
            page_requests += 1
            payload = response.data if isinstance(response.data, dict) else {}
            archive_record = {
                "provider": response.provider,
                "status_code": response.status_code,
                "rate_limit": asdict(response.rate_limit),
                "company_key": company_key(company),
                "company_name": clean_text(company.get("company_name") or ""),
                "company_domain": clean_text(company.get("company_domain") or ""),
                "company_linkedin_url": clean_text(company.get("company_linkedin_url") or ""),
                "role_segment": role_segment.segment_name,
                "requested_titles": list(role_segment.titles),
                "query": query,
                "page": page,
                "data": response.data,
            }
            async with archive_lock:
                append_jsonl(serper_archive_path, archive_record)

            page_hits = extract_linkedin_profile_hits(
                payload=payload,
                company=company,
                role_segment=role_segment,
                query=query,
                page=page,
            )
            if not page_hits:
                empty_pages += 1
                break

            new_in_page = 0
            for hit in page_hits:
                linkedin_url = hit["linkedin_profile_url"]
                compact_hit = {
                    "query": hit["query"],
                    "page": hit["page"],
                    "rank": hit["rank"],
                    "role_segment": hit["role_segment"],
                    "serper_title": hit["serper_title"],
                    "serper_snippet": hit["serper_snippet"],
                }
                record = discovered.get(linkedin_url)
                if record is None:
                    company_payload = {
                        key: clean_text(company.get(key) or "")
                        for key in (
                            "company_description",
                            "company_offer",
                            "company_icp",
                            "company_painpoint",
                            "company_signals",
                            "company_signal_sources",
                        )
                    }
                    discovered[linkedin_url] = {
                        "company_key": company_key(company),
                        "company_name": clean_text(company.get("company_name") or ""),
                        "company_domain": clean_text(company.get("company_domain") or ""),
                        "company_linkedin_url": normalize_linkedin_company_url(
                            clean_text(company.get("company_linkedin_url") or "")
                        ),
                        "company_size": clean_text(company.get("company_size") or ""),
                        "headcount_exact": clean_text(company.get("headcount_exact") or ""),
                        "headcount_range": clean_text(company.get("headcount_range") or ""),
                        "target_people_cap": str(cap),
                        "candidate_pool_target": str(pool_target),
                        "linkedin_profile_url": linkedin_url,
                        "person_name_guess": hit["person_name_guess"],
                        "role_guess": hit["role_guess"],
                        "best_serper_title": hit["serper_title"],
                        "best_serper_snippet": hit["serper_snippet"],
                        "source_role_segments": {hit["role_segment"]},
                        "requested_titles": set(role_segment.titles),
                        "matched_queries": {hit["query"]},
                        "serper_hit_count": 1,
                        "first_seen_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "serper_hits": [compact_hit],
                        **company_payload,
                    }
                    new_in_page += 1
                    continue

                record["source_role_segments"].add(hit["role_segment"])
                record["requested_titles"].update(role_segment.titles)
                record["matched_queries"].add(hit["query"])
                record["serper_hit_count"] += 1
                record["serper_hits"].append(compact_hit)
                current_best_rank = min(int(item["rank"]) for item in record["serper_hits"])
                if int(hit["rank"]) <= int(current_best_rank):
                    record["best_serper_title"] = hit["serper_title"]
                    record["best_serper_snippet"] = hit["serper_snippet"]
                    if hit["person_name_guess"]:
                        record["person_name_guess"] = hit["person_name_guess"]
                    if hit["role_guess"]:
                        record["role_guess"] = hit["role_guess"]

            if new_in_page == 0:
                break

    candidate_rows: list[dict[str, Any]] = []
    for record in discovered.values():
        candidate_rows.append(
            {
                "company_key": record["company_key"],
                "company_name": record["company_name"],
                "company_domain": record["company_domain"],
                "company_linkedin_url": record["company_linkedin_url"],
                "company_size": record["company_size"],
                "headcount_exact": record["headcount_exact"],
                "headcount_range": record["headcount_range"],
                "target_people_cap": record["target_people_cap"],
                "candidate_pool_target": record["candidate_pool_target"],
                "person_name_guess": record["person_name_guess"],
                "role_guess": record["role_guess"],
                "linkedin_profile_url": record["linkedin_profile_url"],
                "source_role_segments": " | ".join(sorted(record["source_role_segments"])),
                "requested_titles": " | ".join(sorted(record["requested_titles"])),
                "matched_queries": " | ".join(sorted(record["matched_queries"])),
                "serper_hit_count": str(record["serper_hit_count"]),
                "first_seen_at": record["first_seen_at"],
                "best_serper_title": record["best_serper_title"],
                "best_serper_snippet": record["best_serper_snippet"],
                "company_description": record["company_description"],
                "company_offer": record["company_offer"],
                "company_icp": record["company_icp"],
                "company_painpoint": record["company_painpoint"],
                "company_signals": record["company_signals"],
                "company_signal_sources": record["company_signal_sources"],
                "serper_hits_json": json.dumps(record["serper_hits"], ensure_ascii=False),
            }
        )

    company_summary = {
        "company_key": company_key(company),
        "company_name": clean_text(company.get("company_name") or ""),
        "company_domain": clean_text(company.get("company_domain") or ""),
        "company_linkedin_url": clean_text(company.get("company_linkedin_url") or ""),
        "headcount_range": clean_text(company.get("headcount_range") or ""),
        "target_people_cap": cap,
        "candidate_pool_target": pool_target,
        "candidate_count": len(candidate_rows),
        "page_requests": page_requests,
        "empty_pages": empty_pages,
    }
    return {"company_summary": company_summary, "candidate_rows": candidate_rows}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    companies_csv = Path(args.companies_csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    serper_archive_path = output_dir / f"{args.base_name}_pages.jsonl"
    candidate_csv_path = output_dir / f"{args.base_name}_candidates.csv"
    candidates_jsonl_path = output_dir / f"{args.base_name}_candidates.jsonl"
    companies_deduped_path = output_dir / f"{args.base_name}_companies_deduped.csv"
    summary_path = output_dir / f"{args.base_name}_summary.json"

    for path in (
        serper_archive_path,
        candidate_csv_path,
        candidates_jsonl_path,
        companies_deduped_path,
        summary_path,
    ):
        if path.exists():
            path.unlink()

    source_rows = read_companies_csv(companies_csv)
    unique_companies = dedupe_companies(source_rows)
    for row in unique_companies:
        row["company_linkedin_url"] = normalize_linkedin_company_url(
            clean_text(row.get("company_linkedin_url") or row.get("linkedin_company_url") or "")
        )
        row["company_domain"] = normalize_domain(clean_text(row.get("company_domain") or ""))
        row["target_people_cap"] = str(target_people_cap(row, args))
        row["candidate_pool_target"] = str(candidate_pool_target(row, args))
        row["company_key"] = company_key(row)

    company_fieldnames = list(unique_companies[0].keys()) if unique_companies else []
    write_csv(companies_deduped_path, unique_companies, company_fieldnames)

    serper_settings = load_serper_settings()
    archive_lock = asyncio.Lock()
    concurrency = asyncio.Semaphore(args.serper_concurrency)

    async with SerperClient(serper_settings) as serper:
        async def run_company(company: dict[str, str]) -> dict[str, Any]:
            async with concurrency:
                return await collect_company_candidates(
                    company=company,
                    role_segments=DEFAULT_ROLE_SEGMENTS,
                    serper=serper,
                    args=args,
                    archive_lock=archive_lock,
                    serper_archive_path=serper_archive_path,
                )

        results = await asyncio.gather(*(run_company(company) for company in unique_companies))

    candidate_rows: list[dict[str, Any]] = []
    company_summaries: list[dict[str, Any]] = []
    for result in results:
        company_summaries.append(result["company_summary"])
        candidate_rows.extend(result["candidate_rows"])

    candidate_rows.sort(
        key=lambda row: (
            row["company_name"].lower(),
            row["source_role_segments"].lower(),
            row["person_name_guess"].lower(),
            row["linkedin_profile_url"].lower(),
        )
    )

    candidate_fieldnames = [
        "company_key",
        "company_name",
        "company_domain",
        "company_linkedin_url",
        "company_size",
        "headcount_exact",
        "headcount_range",
        "target_people_cap",
        "candidate_pool_target",
        "person_name_guess",
        "role_guess",
        "linkedin_profile_url",
        "source_role_segments",
        "requested_titles",
        "matched_queries",
        "serper_hit_count",
        "first_seen_at",
        "best_serper_title",
        "best_serper_snippet",
        "company_description",
        "company_offer",
        "company_icp",
        "company_painpoint",
        "company_signals",
        "company_signal_sources",
        "serper_hits_json",
    ]
    write_csv(candidate_csv_path, candidate_rows, candidate_fieldnames)

    for row in candidate_rows:
        append_jsonl(candidates_jsonl_path, row)

    companies_with_candidates = sum(1 for item in company_summaries if int(item["candidate_count"]) > 0)
    companies_meeting_people_cap = sum(
        1 for item in company_summaries if int(item["candidate_count"]) >= int(item["target_people_cap"])
    )
    companies_meeting_pool_target = sum(
        1 for item in company_summaries if int(item["candidate_count"]) >= int(item["candidate_pool_target"])
    )
    over_200 = sum(1 for company in unique_companies if company_is_over_200(company) is True)
    under_or_eq_200 = len(unique_companies) - over_200
    total_target_people = sum(int(company["target_people_cap"]) for company in unique_companies)
    total_candidate_pool_target = sum(int(company["candidate_pool_target"]) for company in unique_companies)
    total_page_requests = sum(int(item["page_requests"]) for item in company_summaries)
    summary = {
        "source_rows": len(source_rows),
        "unique_companies": len(unique_companies),
        "over_200_companies": over_200,
        "under_or_eq_200_companies": under_or_eq_200,
        "target_people_total": total_target_people,
        "candidate_pool_target_total": total_candidate_pool_target,
        "role_segment_count": len(DEFAULT_ROLE_SEGMENTS),
        "max_pages_per_query": args.max_pages,
        "results_per_page": args.num,
        "total_serper_page_requests": total_page_requests,
        "estimated_serper_cost_usd": round(total_page_requests * SERPER_COST_PER_SEARCH_USD, 2),
        "companies_with_candidates": companies_with_candidates,
        "companies_meeting_people_cap_by_serper_candidates": companies_meeting_people_cap,
        "companies_meeting_candidate_pool_target": companies_meeting_pool_target,
        "candidate_rows": len(candidate_rows),
        "output_files": {
            "companies_deduped_csv": str(companies_deduped_path),
            "candidates_csv": str(candidate_csv_path),
            "candidates_jsonl": str(candidates_jsonl_path),
            "serper_pages_jsonl": str(serper_archive_path),
            "summary_json": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    args = parse_args()
    summary = asyncio.run(run(args))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
