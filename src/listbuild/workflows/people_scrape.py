from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlparse

from listbuild.budget import RunBudgetTracker, ScrapePricing
from listbuild.datasets import PersonRow, person_row_from_harvest_profile, write_person_rows_csv
from listbuild.jsonl import append_jsonl
from listbuild.providers import HarvestClient, SerperClient
from listbuild.runs import RunPaths, build_run_paths, scaffold_run_directories
from listbuild.storage import (
    build_person_record_key,
    fetch_existing_person_record_keys,
    sync_run_to_supabase,
)


@dataclass(frozen=True, slots=True)
class RoleSearchSegment:
    segment_name: str
    target_titles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PeopleScrapePolicy:
    max_people_per_company: int = 10
    max_people_per_company_under_200: int | None = None
    max_people_per_company_over_200: int | None = None
    search_results_per_query: int = 10
    posted_limit: str = "month"
    include_email: bool = False
    use_main_profile: bool = False
    require_current_employer_match: bool = False
    use_company_domain_in_query: bool = False
    dedupe_against_supabase: bool = True
    upsert_supabase: bool = False


@dataclass(slots=True)
class PeopleScrapeRun:
    run_slug: str
    people_count: int
    output_csv: str
    raw_serper_jsonl: str
    raw_harvest_profile_jsonl: str
    supabase: dict[str, int] | None = None
    budget: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "people_count": self.people_count,
            "output_csv": self.output_csv,
            "raw_serper_jsonl": self.raw_serper_jsonl,
            "raw_harvest_profile_jsonl": self.raw_harvest_profile_jsonl,
            "supabase": self.supabase,
            "budget": self.budget,
        }


class PeopleScrapeWorkflow:
    def __init__(self, serper: SerperClient, harvest: HarvestClient) -> None:
        self._serper = serper
        self._harvest = harvest

    @staticmethod
    def _normalize_text(value: str) -> str:
        lowered = value.strip().lower()
        if not lowered:
            return ""
        collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
        return " ".join(collapsed.split())

    @classmethod
    def _company_match(cls, left: str, right: str) -> bool:
        left_norm = cls._normalize_text(left)
        right_norm = cls._normalize_text(right)
        if not left_norm or not right_norm:
            return False
        return left_norm == right_norm or left_norm in right_norm or right_norm in left_norm

    @staticmethod
    def _extract_current_company(payload: dict[str, Any]) -> str:
        element = payload.get("element")
        if not isinstance(element, dict):
            return ""
        current_position = element.get("currentPosition")
        if isinstance(current_position, list) and current_position:
            first_position = current_position[0]
            if isinstance(first_position, dict):
                company_name = str(first_position.get("companyName") or "").strip()
                if company_name:
                    return company_name
        current_company = element.get("currentCompany")
        if isinstance(current_company, dict):
            company_name = str(current_company.get("name") or "").strip()
            if company_name:
                return company_name
        return ""

    @staticmethod
    def _range_bounds(raw_value: str) -> tuple[int | None, int | None]:
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

    @classmethod
    def _company_is_over_200(cls, company: dict[str, str]) -> bool | None:
        for key in ("headcount_range", "company_headcount_range", "company_size"):
            start, end = cls._range_bounds(str(company.get(key) or ""))
            if start is None and end is None:
                continue
            if start is not None and start > 200:
                return True
            if end is not None:
                return end > 200
        for key in ("headcount_exact", "company_headcount_exact"):
            raw_value = str(company.get(key) or "").strip()
            if not raw_value:
                continue
            try:
                return int(raw_value) > 200
            except ValueError:
                continue
        return None

    @classmethod
    def _max_people_for_company(
        cls,
        company: dict[str, str],
        policy: PeopleScrapePolicy,
    ) -> int:
        over_200 = cls._company_is_over_200(company)
        if over_200 is True and policy.max_people_per_company_over_200 is not None:
            return policy.max_people_per_company_over_200
        if over_200 is False and policy.max_people_per_company_under_200 is not None:
            return policy.max_people_per_company_under_200
        return policy.max_people_per_company

    @staticmethod
    def _quote_query_term(value: str) -> str:
        return '"' + value.replace('"', "") + '"'

    @classmethod
    def _role_search_query(
        cls,
        company: dict[str, str],
        role_segment: RoleSearchSegment,
        policy: PeopleScrapePolicy,
    ) -> str:
        company_name = str(company.get("company_name") or "").strip()
        company_domain = str(company.get("company_domain") or "").strip()
        titles = " OR ".join(cls._quote_query_term(title) for title in role_segment.target_titles if title.strip())
        company_clause = cls._quote_query_term(company_name)
        if policy.use_company_domain_in_query and company_domain:
            company_clause = f"({cls._quote_query_term(company_name)} OR {cls._quote_query_term(company_domain)})"
        return f"site:linkedin.com/in/ {company_clause} ({titles})"

    @staticmethod
    def _candidate_batch_size(remaining_slots: int, policy: PeopleScrapePolicy) -> int:
        multiplier = 2 if policy.require_current_employer_match else 1
        return max(remaining_slots * multiplier, remaining_slots, 1)

    async def run(
        self,
        *,
        seller: str,
        segment: str,
        companies_csv: str | Path,
        role_segments: list[RoleSearchSegment],
        date_stamp: str | None = None,
        root: str = "runs",
        policy: PeopleScrapePolicy | None = None,
        budget_tracker: RunBudgetTracker | None = None,
        pricing: ScrapePricing | None = None,
    ) -> PeopleScrapeRun:
        policy = policy or PeopleScrapePolicy()
        paths = build_run_paths(
            seller=seller,
            segment=segment,
            date_stamp=date_stamp,
            root=root,
        )
        scaffold_run_directories(paths)
        tracker = budget_tracker or RunBudgetTracker(
            run_slug=paths.naming.run_slug,
            events_path=paths.budget_jsonl,
            summary_path=paths.budget_summary,
            pricing=pricing,
        )
        source_companies_csv = Path(companies_csv)
        if source_companies_csv.resolve() != paths.company_csv.resolve():
            shutil.copyfile(source_companies_csv, paths.company_csv)
        company_rows = self._read_companies_csv(companies_csv)
        people_rows = await self._enrich_people(
            paths=paths,
            companies=company_rows,
            role_segments=role_segments,
            policy=policy,
            budget_tracker=tracker,
        )
        write_person_rows_csv(people_rows, paths.people_csv)
        supabase_result: dict[str, int] | None = None
        if policy.upsert_supabase:
            supabase_result = sync_run_to_supabase(
                paths=paths,
                metadata={
                    "workflow": "people-scrape",
                    "companies_csv": str(companies_csv),
                    "people_count": len(people_rows),
                    "require_current_employer_match": policy.require_current_employer_match,
                    "use_company_domain_in_query": policy.use_company_domain_in_query,
                    "max_people_per_company": policy.max_people_per_company,
                    "max_people_per_company_under_200": policy.max_people_per_company_under_200,
                    "max_people_per_company_over_200": policy.max_people_per_company_over_200,
                    "role_segments": [
                        {
                            "segment_name": segment.segment_name,
                            "target_titles": list(segment.target_titles),
                        }
                        for segment in role_segments
                    ],
                },
                people_rows_override=[row.to_storage_dict() for row in people_rows],
            )
        budget_summary = tracker.write_summary()
        return PeopleScrapeRun(
            run_slug=paths.naming.run_slug,
            people_count=len(people_rows),
            output_csv=str(paths.people_csv),
            raw_serper_jsonl=str(paths.serper_jsonl),
            raw_harvest_profile_jsonl=str(paths.harvest_profile_jsonl),
            supabase=supabase_result,
            budget=budget_summary,
        )

    @staticmethod
    def _read_companies_csv(path: str | Path) -> list[dict[str, str]]:
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    async def _enrich_people(
        self,
        *,
        paths: RunPaths,
        companies: list[dict[str, str]],
        role_segments: list[RoleSearchSegment],
        policy: PeopleScrapePolicy,
        budget_tracker: RunBudgetTracker,
    ) -> list[PersonRow]:
        semaphore = asyncio.Semaphore(8)
        people_rows: list[PersonRow] = []

        async def search_role(company: dict[str, str], role_segment: RoleSearchSegment) -> list[str]:
            company_name = company.get("company_name", "")
            query = self._role_search_query(company, role_segment, policy)
            response = await self._serper.search(query, num=policy.search_results_per_query)
            budget_tracker.record_serper_search(
                stage="people_discovery",
                query=query,
                num=policy.search_results_per_query,
            )
            append_jsonl(
                paths.serper_jsonl,
                {
                    "provider": "serper",
                    "stage": "people_discovery",
                    "query": query,
                    "company_name": company_name,
                    "role_segment": role_segment.segment_name,
                    "response": response.data,
                },
            )
            urls = self._extract_linkedin_profile_urls(response.data)
            if not policy.dedupe_against_supabase:
                return urls
            keys = [
                build_person_record_key(person_linkedin_url=linkedin_url)
                for linkedin_url in urls
            ]
            existing = fetch_existing_person_record_keys(keys)
            if not existing:
                return urls
            return [
                linkedin_url
                for linkedin_url in urls
                if build_person_record_key(person_linkedin_url=linkedin_url) not in existing
            ]

        async def hydrate_profile(
            company: dict[str, str],
            role_segment: RoleSearchSegment,
            linkedin_url: str,
        ) -> PersonRow | None:
            async with semaphore:
                profile_response = await self._harvest.get_profile(
                    url=linkedin_url,
                    main=policy.use_main_profile,
                    find_email=policy.include_email,
                    include_about_profile=True,
                )
                budget_tracker.record_harvest_profile_get(
                    stage="profile_get",
                    linkedin_url=linkedin_url,
                    include_email=policy.include_email,
                    use_main_profile=policy.use_main_profile,
                )
                profile_payload = profile_response.data if isinstance(profile_response.data, dict) else {}
                parsed_current_company = self._extract_current_company(profile_payload)
                employer_match = self._company_match(company.get("company_name", ""), parsed_current_company)
                append_jsonl(
                    paths.harvest_profile_jsonl,
                    {
                        "provider": "harvest",
                        "stage": "profile_get",
                        "company_name": company.get("company_name", ""),
                        "company_domain": company.get("company_domain", ""),
                        "role_segment": role_segment.segment_name,
                        "linkedin_url": linkedin_url,
                        "parsed_current_company": parsed_current_company,
                        "current_employer_match": "yes" if employer_match else "no",
                        "response": profile_response.data,
                    },
                )
                if policy.require_current_employer_match and not employer_match:
                    return None

                posts_response = await self._harvest.profile_posts(
                    profile=linkedin_url,
                    posted_limit=policy.posted_limit,
                    page=1,
                )
                budget_tracker.record_harvest_profile_posts(
                    stage="profile_posts",
                    linkedin_url=linkedin_url,
                    page=1,
                )
                append_jsonl(
                    paths.harvest_profile_jsonl,
                    {
                        "provider": "harvest",
                        "stage": "profile_posts",
                        "company_name": company.get("company_name", ""),
                        "company_domain": company.get("company_domain", ""),
                        "role_segment": role_segment.segment_name,
                        "linkedin_url": linkedin_url,
                        "response": posts_response.data,
                    },
                )
                posts_payload = posts_response.data if isinstance(posts_response.data, dict) else {}
                row = person_row_from_harvest_profile(
                    profile_payload,
                    naming=paths.naming,
                    company_name=company.get("company_name", ""),
                    company_domain=company.get("company_domain", ""),
                    source_role_segment=role_segment.segment_name,
                    posts_payload=posts_payload,
                )
                profile_email = self._extract_email(profile_payload)
                return PersonRow(
                    **{
                        **row.to_storage_dict(),
                        "person_email": profile_email,
                        "person_email_status": "found" if profile_email else "",
                    }
                )

        for company in companies:
            company_seen: set[str] = set()
            company_people: list[PersonRow] = []
            target_people = self._max_people_for_company(company, policy)
            if target_people <= 0:
                continue
            for role_segment in role_segments:
                if len(company_people) >= target_people:
                    break
                urls = await search_role(company, role_segment)
                pending_urls: list[str] = []
                for linkedin_url in urls:
                    if linkedin_url in company_seen:
                        continue
                    company_seen.add(linkedin_url)
                    pending_urls.append(linkedin_url)

                cursor = 0
                while cursor < len(pending_urls) and len(company_people) < target_people:
                    remaining_slots = target_people - len(company_people)
                    batch_size = min(
                        len(pending_urls) - cursor,
                        self._candidate_batch_size(remaining_slots, policy),
                    )
                    batch_urls = pending_urls[cursor : cursor + batch_size]
                    cursor += batch_size
                    batch_rows = await asyncio.gather(
                        *(hydrate_profile(company, role_segment, linkedin_url) for linkedin_url in batch_urls)
                    )
                    for row in batch_rows:
                        if row is None:
                            continue
                        company_people.append(row)
                        if len(company_people) >= target_people:
                            break
            people_rows.extend(company_people[:target_people])
        return people_rows

    @staticmethod
    def _extract_linkedin_profile_urls(payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        organic = payload.get("organic")
        if not isinstance(organic, list):
            return []
        urls: list[str] = []
        for item in organic:
            if not isinstance(item, dict):
                continue
            candidate = item.get("link") or item.get("url")
            if not isinstance(candidate, str):
                continue
            parsed = urlparse(candidate)
            if "linkedin.com" not in parsed.netloc.lower():
                continue
            if "/in/" not in parsed.path.lower():
                continue
            urls.append(candidate)
        return urls

    @staticmethod
    def _extract_email(profile_payload: dict[str, Any]) -> str:
        element = profile_payload.get("element")
        if not isinstance(element, dict):
            return ""
        for key in ("email", "workEmail"):
            value = element.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
