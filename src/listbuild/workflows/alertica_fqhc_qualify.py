from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from listbuild.budget import RunBudgetTracker, ScrapePricing
from listbuild.datasets import _build_recent_posts_summary
from listbuild.jsonl import append_jsonl
from listbuild.normalize import normalize_url
from listbuild.providers import HarvestClient
from listbuild.runs import RunPaths, build_run_paths, scaffold_run_directories
from listbuild.storage import (
    build_company_record_key,
    build_person_record_key,
    fetch_existing_company_record_keys,
    fetch_existing_person_record_keys,
)


FQHC_TIER = "1A_fqhc"
TECHNICAL_PATTERNS = (
    r"\bcio\b",
    r"\bchief information officer\b",
    r"\bcto\b",
    r"\bchief technology officer\b",
    r"\bdirector of it\b",
    r"\bdirector of information technology\b",
    r"\bit director\b",
    r"\bit manager\b",
    r"\binformation technology manager\b",
    r"\bhead of it\b",
    r"\bsys(?:tem)?\s*admin\b",
    r"\bsystems administrator\b",
    r"\bnetwork administrator\b",
    r"\binfrastructure\b",
    r"\bsecurity officer\b",
)
EXECUTIVE_PATTERNS = (
    r"\bceo\b",
    r"\bchief executive officer\b",
    r"\bpresident\b",
    r"\bexecutive director\b",
    r"\bowner\b",
    r"\bfounder\b",
    r"\bmanaging director\b",
)
OPERATIONS_PATTERNS = (
    r"\bcoo\b",
    r"\bchief operating officer\b",
    r"\bdirector of operations\b",
    r"\boperations director\b",
    r"\boperations manager\b",
    r"\bgeneral manager\b",
)
LEGAL_PATTERNS = (
    r"\bgeneral counsel\b",
    r"\battorney\b",
    r"\blegal\b",
    r"\bcounsel\b",
)
SECURITY_RISK_PATTERNS = (
    r"\bciso\b",
    r"\bchief compliance officer\b",
    r"\bchief risk officer\b",
    r"\bcompliance\b",
    r"\brisk\b",
    r"\bsecurity officer\b",
    r"\bcyber ?security\b",
)
CLINICAL_ADMIN_PATTERNS = (
    r"\bchief medical officer\b",
    r"\bmedical director\b",
    r"\bclinical director\b",
    r"\bphysician\b",
    r"\bnurse practitioner\b",
    r"\bpractice administrator\b",
)
HR_ADMIN_PATTERNS = (
    r"\bhr director\b",
    r"\bdirector of hr\b",
    r"\bpeople operations\b",
    r"\bhuman resources\b",
)
GENERIC_MANAGEMENT_PATTERNS = (
    r"\bdirector\b",
    r"\bmanager\b",
    r"\badministrator\b",
    r"\bchief\b",
    r"\bhead of\b",
    r"\bvice president\b",
    r"\bsupervisor\b",
    r"\bcoordinator\b",
    r"\bprogram director\b",
    r"\bsite director\b",
    r"\boffice manager\b",
    r"\bclinic manager\b",
    r"\bpractice manager\b",
)
LEADERSHIP_ROLE_GROUPS = {
    "Executive / Owner",
    "Operations",
    "Finance",
    "Technical",
    "Legal",
    "Security / Compliance",
    "Risk",
}
ROLE_GROUP_BUCKETS = {
    "Technical": ("technical_it", 10),
    "Finance": ("finance", 20),
    "Security / Compliance": ("security_compliance_risk", 30),
    "Risk": ("security_compliance_risk", 31),
    "Operations": ("operations", 40),
    "Executive / Owner": ("executive_owner", 50),
    "Legal": ("legal", 60),
}
FINANCE_PATTERNS = (
    r"\bcfo\b",
    r"\bcontroller\b",
    r"\bvp finance\b",
    r"\bdirector of finance\b",
    r"\bfinance director\b",
    r"\bchief financial officer\b",
)


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _parse_domain(value: str) -> str:
    normalized = normalize_url(value or "")
    if normalized is not None:
        return normalized.host
    candidate = (value or "").strip().lower()
    return candidate.removeprefix("https://").removeprefix("http://").removeprefix("www.")


def _matches_any_pattern(value: str, patterns: tuple[str, ...]) -> bool:
    text = value or ""
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _title_matches_persona(title: str, persona: str) -> bool:
    if persona == "all_current_fqhc":
        return _title_matches_persona(title, "broad_fqhc")
    if persona == "technical_it":
        return _matches_any_pattern(title, TECHNICAL_PATTERNS)
    if persona == "finance":
        return _matches_any_pattern(title, FINANCE_PATTERNS)
    if persona == "all":
        return _title_matches_persona(title, "technical_it") or _title_matches_persona(
            title,
            "finance",
        )
    if persona == "leadership":
        return any(
            _matches_any_pattern(title, patterns)
            for patterns in (
                TECHNICAL_PATTERNS,
                FINANCE_PATTERNS,
                SECURITY_RISK_PATTERNS,
                OPERATIONS_PATTERNS,
                EXECUTIVE_PATTERNS,
                LEGAL_PATTERNS,
            )
        )
    if persona == "broad_fqhc":
        return any(
            _matches_any_pattern(title, patterns)
            for patterns in (
                TECHNICAL_PATTERNS,
                FINANCE_PATTERNS,
                SECURITY_RISK_PATTERNS,
                OPERATIONS_PATTERNS,
                EXECUTIVE_PATTERNS,
                LEGAL_PATTERNS,
                CLINICAL_ADMIN_PATTERNS,
                HR_ADMIN_PATTERNS,
                GENERIC_MANAGEMENT_PATTERNS,
            )
        )
    raise ValueError(f"Unsupported persona: {persona}")


def _title_priority(title: str, persona: str) -> int:
    if persona == "all_current_fqhc":
        return _title_priority(title, "broad_fqhc")
    normalized = _normalize_text(title)
    if persona == "technical_it":
        if "cio" in normalized or "chief information officer" in normalized:
            return 1
        if "cto" in normalized or "chief technology officer" in normalized:
            return 1
        if (
            "director of it" in normalized
            or "it director" in normalized
            or "director of information technology" in normalized
        ):
            return 2
        if "head of it" in normalized:
            return 3
        if "it manager" in normalized or "information technology manager" in normalized:
            return 4
        if "sys admin" in normalized or "systems administrator" in normalized:
            return 5
        return 9
    if persona == "finance":
        if "chief financial officer" in normalized or normalized == "cfo":
            return 1
        if "vp finance" in normalized:
            return 2
        if "controller" in normalized:
            return 3
        if "director of finance" in normalized or "finance director" in normalized:
            return 4
        return 9
    if persona == "leadership":
        bucket = _detect_fqhc_bucket_from_text(normalized)
        return bucket[1] if bucket is not None else 99
    if persona == "broad_fqhc":
        bucket = _detect_fqhc_bucket_from_text(normalized, broad=True)
        return bucket[1] if bucket is not None else 99
    return min(_title_priority(title, "technical_it"), _title_priority(title, "finance"))


def _company_match(left: str, right: str) -> bool:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm or left_norm in right_norm or right_norm in left_norm


def _extract_current_company(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    current_position = element.get("currentPosition")
    if isinstance(current_position, list) and current_position:
        first_position = current_position[0]
        if isinstance(first_position, dict):
            return str(first_position.get("companyName") or "").strip()
    current_company = element.get("currentCompany")
    if isinstance(current_company, dict):
        return str(current_company.get("name") or "").strip()
    return ""


def _extract_profile_skills(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    skills = element.get("skills")
    if not isinstance(skills, list):
        return ""
    names = []
    for item in skills[:10]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return " | ".join(names)


def _extract_company_headcount_range(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    employee_count_range = element.get("employeeCountRange")
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


def _compose_row_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "").strip()
        for key in ("parsed_title", "raw_serp_title", "raw_snippet")
    ).strip()


def _detect_fqhc_bucket_from_text(
    text: str,
    *,
    broad: bool = False,
) -> tuple[str, int] | None:
    checks: list[tuple[str, tuple[str, ...], int]] = [
        ("technical_it", TECHNICAL_PATTERNS, 10),
        ("finance", FINANCE_PATTERNS, 20),
        ("security_compliance_risk", SECURITY_RISK_PATTERNS, 30),
        ("operations", OPERATIONS_PATTERNS, 40),
        ("executive_owner", EXECUTIVE_PATTERNS, 50),
        ("legal", LEGAL_PATTERNS, 60),
    ]
    if broad:
        checks.extend(
            [
                ("clinical_admin", CLINICAL_ADMIN_PATTERNS, 70),
                ("hr_admin", HR_ADMIN_PATTERNS, 80),
                ("generic_management", GENERIC_MANAGEMENT_PATTERNS, 90),
            ]
        )
    for bucket, patterns, priority in checks:
        if _matches_any_pattern(text, patterns):
            return bucket, priority
    return None


def _write_rows(path: str | Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "" if value is None else value
                    for key, value in row.items()
                    if key in fieldnames
                }
            )


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
    return records


@dataclass(frozen=True, slots=True)
class AlerticaFqhcQualifierPolicy:
    companies_input: str
    people_input: str
    persona: str = "technical_it"
    exclude_people_csv: str = ""
    exclude_companies_csv: str = ""
    exclude_supabase: bool = True
    max_companies: int = 250
    max_people: int = 1000
    posted_limit: str = "month"
    include_company_posts: bool = True
    include_profile_posts: bool = True
    run_company_harvest: bool = True
    run_people_harvest: bool = True
    run_harvest: bool = True


@dataclass(slots=True)
class AlerticaFqhcQualifierRun:
    run_slug: str
    company_seed_count: int
    people_seed_count: int
    company_seed_csv: str
    people_seed_csv: str
    company_qualifier_csv: str
    people_qualifier_csv: str
    raw_harvest_company_jsonl: str
    raw_harvest_profile_jsonl: str
    budget: dict[str, Any] | None = None
    rubric: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "company_seed_count": self.company_seed_count,
            "people_seed_count": self.people_seed_count,
            "company_seed_csv": self.company_seed_csv,
            "people_seed_csv": self.people_seed_csv,
            "company_qualifier_csv": self.company_qualifier_csv,
            "people_qualifier_csv": self.people_qualifier_csv,
            "raw_harvest_company_jsonl": self.raw_harvest_company_jsonl,
            "raw_harvest_profile_jsonl": self.raw_harvest_profile_jsonl,
            "budget": self.budget,
            "rubric": self.rubric,
        }


class AlerticaFqhcQualifierWorkflow:
    def __init__(self, harvest: HarvestClient) -> None:
        self._harvest = harvest

    async def run(
        self,
        *,
        seller: str,
        segment: str,
        date_stamp: str | None = None,
        root: str = "runs",
        policy: AlerticaFqhcQualifierPolicy,
        pricing: ScrapePricing | None = None,
    ) -> AlerticaFqhcQualifierRun:
        paths = build_run_paths(
            seller=seller,
            segment=segment,
            date_stamp=date_stamp,
            root=root,
        )
        scaffold_run_directories(paths)
        tracker = RunBudgetTracker(
            run_slug=paths.naming.run_slug,
            events_path=paths.budget_jsonl,
            summary_path=paths.budget_summary,
            pricing=pricing,
        )

        company_rows = self._read_csv(policy.companies_input)
        people_rows = self._read_csv(policy.people_input)

        selected_companies, selected_people = self._select_seed_rows(
            company_rows=company_rows,
            people_rows=people_rows,
            persona=policy.persona,
            exclude_people_csv=policy.exclude_people_csv,
            exclude_companies_csv=policy.exclude_companies_csv,
            exclude_supabase=policy.exclude_supabase,
            max_companies=policy.max_companies,
            max_people=policy.max_people,
        )

        company_seed_csv = paths.company_csv
        people_seed_csv = paths.people_csv
        company_qualifier_csv = paths.output_dir / f"{paths.naming.run_slug}-company-harvest-qualifier.csv"
        people_qualifier_csv = paths.output_dir / f"{paths.naming.run_slug}-people-harvest-qualifier.csv"

        _write_rows(company_seed_csv, selected_companies, list(selected_companies[0].keys()) if selected_companies else [
            "linkedin_slug",
            "linkedin_url",
            "company_name",
            "tier",
            "tier_label",
            "primary_keyword",
            "size_bracket",
            "description_snippet",
            "discovery_query",
            "page_found",
            "first_seen_at",
            "resolved_domain",
            "resolve_status",
            "matching_people_count",
        ])
        _write_rows(people_seed_csv, selected_people, list(selected_people[0].keys()) if selected_people else [
            "person_slug",
            "person_linkedin_url",
            "first_name",
            "last_name",
            "parsed_title",
            "parsed_current_company",
            "current_employer_match",
            "role_group",
            "role_group_label",
            "priority_tier",
            "source_company_slug",
            "source_company_name",
            "source_company_tier",
            "source_company_tier_label",
            "source_company_size",
            "source_company_domain",
            "rubric_persona",
            "rubric_title_match",
            "title_priority",
        ])

        company_qual_rows: list[dict[str, Any]] = []
        people_qual_rows: list[dict[str, Any]] = []
        if policy.run_harvest and policy.run_company_harvest:
            company_qual_rows = await self._qualify_companies(
                paths=paths,
                companies=selected_companies,
                posted_limit=policy.posted_limit,
                include_company_posts=policy.include_company_posts,
                budget_tracker=tracker,
            )
            _write_rows(
                company_qualifier_csv,
                company_qual_rows,
                list(company_qual_rows[0].keys()) if company_qual_rows else [
                    "linkedin_slug",
                    "company_name",
                    "harvest_company_name",
                ],
            )
        company_lookup = {
            row["linkedin_slug"]: row
            for row in company_qual_rows
            if row.get("linkedin_slug")
        }
        if policy.run_harvest and policy.run_people_harvest:
            people_qual_rows = await self._qualify_people(
                paths=paths,
                people=selected_people,
                company_lookup=company_lookup,
                persona=policy.persona,
                posted_limit=policy.posted_limit,
                include_profile_posts=policy.include_profile_posts,
                budget_tracker=tracker,
            )
            _write_rows(
                people_qualifier_csv,
                people_qual_rows,
                list(people_qual_rows[0].keys()) if people_qual_rows else [
                    "person_linkedin_url",
                    "full_name",
                    "harvest_headline",
                ],
            )

        budget_summary = tracker.write_summary()
        rubric = {
            "tier": FQHC_TIER,
            "persona": policy.persona,
            "approved_personas": {
                "technical_it": [
                    "Director of IT",
                    "IT Manager",
                    "Head of IT",
                    "CIO",
                    "CTO",
                    "Sys Admin",
                ],
                "finance": [
                    "CFO",
                    "Controller",
                    "VP Finance",
                    "Director of Finance",
                ],
            },
            "broader_buckets": [
                "technical_it",
                "finance",
                "security_compliance_risk",
                "operations",
                "executive_owner",
                "legal",
                "clinical_admin",
                "hr_admin",
                "generic_management",
            ],
            "seed_mode": "all current-employer FQHC rows" if policy.persona == "all_current_fqhc" else "title-filtered corpus",
        }
        return AlerticaFqhcQualifierRun(
            run_slug=paths.naming.run_slug,
            company_seed_count=len(selected_companies),
            people_seed_count=len(selected_people),
            company_seed_csv=str(company_seed_csv),
            people_seed_csv=str(people_seed_csv),
            company_qualifier_csv=str(company_qualifier_csv),
            people_qualifier_csv=str(people_qualifier_csv),
            raw_harvest_company_jsonl=str(paths.harvest_company_jsonl),
            raw_harvest_profile_jsonl=str(paths.harvest_profile_jsonl),
            budget=budget_summary,
            rubric=rubric,
        )

    @staticmethod
    def _read_csv(path: str | Path) -> list[dict[str, str]]:
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _select_seed_rows(
        self,
        *,
        company_rows: list[dict[str, str]],
        people_rows: list[dict[str, str]],
        persona: str,
        exclude_people_csv: str,
        exclude_companies_csv: str,
        exclude_supabase: bool,
        max_companies: int,
        max_people: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        excluded_company_keys = self._load_excluded_company_keys(exclude_companies_csv)
        excluded_person_keys = self._load_excluded_person_keys(exclude_people_csv)
        if exclude_supabase:
            excluded_company_keys |= self._fetch_supabase_company_keys(company_rows)
            excluded_person_keys |= self._fetch_supabase_person_keys(people_rows)
        fqhc_people = []
        for row in people_rows:
            if (row.get("source_company_tier") or "").strip() != FQHC_TIER:
                continue
            if (row.get("current_employer_match") or "").strip().lower() != "yes":
                continue
            company_key = self._source_company_key(row)
            if company_key and company_key in excluded_company_keys:
                continue
            person_key = self._source_person_key(row)
            if person_key and person_key in excluded_person_keys:
                continue
            title = (row.get("parsed_title") or "").strip()
            role_group = (row.get("role_group_label") or "").strip()
            selection_bucket = ""
            selection_priority = 99
            title_match = _title_matches_persona(title, persona)

            if persona == "all_current_fqhc":
                title_match = True
                if role_group in ROLE_GROUP_BUCKETS:
                    selection_bucket, selection_priority = ROLE_GROUP_BUCKETS[role_group]
                else:
                    text = _compose_row_text(row)
                    detected = _detect_fqhc_bucket_from_text(text, broad=True)
                    if detected is not None:
                        selection_bucket, selection_priority = detected
                    else:
                        selection_bucket, selection_priority = "generic_management", 99

            if persona == "technical_it" and role_group == "Technical":
                title_match = True
                selection_bucket = "technical_it"
                selection_priority = 10
            elif persona == "finance" and role_group == "Finance":
                title_match = True
                selection_bucket = "finance"
                selection_priority = 20
            elif persona == "all" and role_group in {"Technical", "Finance"}:
                title_match = True
                selection_bucket = "technical_it" if role_group == "Technical" else "finance"
                selection_priority = 10 if role_group == "Technical" else 20
            elif persona in {"leadership", "broad_fqhc"} and role_group in ROLE_GROUP_BUCKETS:
                selection_bucket, selection_priority = ROLE_GROUP_BUCKETS[role_group]
                title_match = True
            elif persona in {"leadership", "broad_fqhc"}:
                text = _compose_row_text(row)
                detected = _detect_fqhc_bucket_from_text(
                    text,
                    broad=persona == "broad_fqhc",
                )
                if detected is not None:
                    selection_bucket, selection_priority = detected
                    title_match = True

            if not title_match:
                continue
            if not selection_bucket:
                if persona == "technical_it":
                    selection_bucket, selection_priority = "technical_it", 10
                elif persona == "finance":
                    selection_bucket, selection_priority = "finance", 20
                elif persona == "all":
                    detected = _detect_fqhc_bucket_from_text(title)
                    if detected is not None:
                        selection_bucket, selection_priority = detected
            fqhc_people.append(
                {
                    **row,
                    "source_company_domain": "",
                    "rubric_persona": persona,
                    "rubric_title_match": "true",
                    "selection_bucket": selection_bucket or role_group.lower().replace(" / ", "_").replace(" ", "_"),
                    "selection_priority": str(selection_priority),
                    "title_priority": str(
                        selection_priority
                        if persona in {"leadership", "broad_fqhc"}
                        else _title_priority(title, persona)
                    ),
                }
            )

        fqhc_people.sort(
            key=lambda row: (
                int(row.get("selection_priority") or "99"),
                int(row.get("title_priority") or "99"),
                row.get("source_company_name") or "",
                row.get("last_name") or "",
                row.get("first_name") or "",
            )
        )
        people_by_company: dict[str, int] = {}
        for row in fqhc_people:
            slug = row.get("source_company_slug") or ""
            people_by_company[slug] = people_by_company.get(slug, 0) + 1

        fqhc_companies = []
        company_rows_by_slug: dict[str, dict[str, str]] = {}
        for row in company_rows:
            if (row.get("tier") or "").strip() != FQHC_TIER:
                continue
            slug = (row.get("linkedin_slug") or "").strip()
            if not slug:
                continue
            company_key = self._company_row_key(row)
            if company_key and company_key in excluded_company_keys:
                continue
            company_rows_by_slug[slug] = row

        ranked_company_slugs = sorted(
            (
                slug
                for slug in company_rows_by_slug.keys()
                if people_by_company.get(slug, 0) > 0
            ),
            key=lambda slug: (
                -people_by_company.get(slug, 0),
                company_rows_by_slug[slug].get("company_name") or "",
            ),
        )
        if max_companies > 0:
            ranked_company_slugs = ranked_company_slugs[:max_companies]
        selected_company_slugs = set(ranked_company_slugs)

        for slug in ranked_company_slugs:
            row = company_rows_by_slug[slug]
            fqhc_companies.append(
                {
                    **row,
                    "matching_people_count": str(people_by_company.get(slug, 0)),
                }
            )

        if max_people == 0:
            return fqhc_companies, []

        selected_people: list[dict[str, Any]] = []
        for row in fqhc_people:
            if row.get("source_company_slug") not in selected_company_slugs:
                continue
            company_row = company_rows_by_slug.get(row.get("source_company_slug") or "", {})
            selected_people.append(
                {
                    **row,
                    "source_company_domain": _parse_domain(
                        company_row.get("resolved_domain") or ""
                    ),
                }
            )
            if max_people > 0 and len(selected_people) >= max_people:
                break
        return fqhc_companies, selected_people

    @staticmethod
    def _company_row_key(row: dict[str, Any]) -> str:
        linkedin_url = str(row.get("linkedin_url") or row.get("company_linkedin_url") or "").strip()
        domain = _parse_domain(str(row.get("resolved_domain") or row.get("company_domain") or ""))
        company_name = str(row.get("company_name") or "").strip()
        return build_company_record_key(
            company_linkedin_url=linkedin_url,
            company_domain=domain,
            company_name=company_name,
        )

    @staticmethod
    def _source_company_key(row: dict[str, Any]) -> str:
        domain = _parse_domain(str(row.get("source_company_domain") or ""))
        company_name = str(row.get("source_company_name") or "").strip()
        return build_company_record_key(
            company_domain=domain,
            company_name=company_name,
        )

    @staticmethod
    def _source_person_key(row: dict[str, Any]) -> str:
        linkedin_url = str(row.get("person_linkedin_url") or "").strip()
        email = str(row.get("person_email") or "").strip().lower()
        company_domain = _parse_domain(str(row.get("source_company_domain") or ""))
        full_name = " ".join(
            part for part in [str(row.get("first_name") or "").strip(), str(row.get("last_name") or "").strip()] if part
        ).strip()
        return build_person_record_key(
            person_linkedin_url=linkedin_url,
            person_email=email,
            company_domain=company_domain,
            person_full_name=full_name,
        )

    @classmethod
    def _load_excluded_company_keys(cls, path: str) -> set[str]:
        if not path:
            return set()
        rows = cls._read_csv(path)
        keys: set[str] = set()
        for row in rows:
            linkedin_url = str(row.get("company_linkedin_url") or row.get("linkedin_url") or "").strip()
            domain = _parse_domain(str(row.get("company_domain") or row.get("resolved_domain") or row.get("source_company_domain") or ""))
            company_name = str(
                row.get("company_name_raw")
                or row.get("source_company_name")
                or row.get("company_name")
                or ""
            ).strip()
            key = build_company_record_key(
                company_linkedin_url=linkedin_url,
                company_domain=domain,
                company_name=company_name,
            )
            if key:
                keys.add(key)
        return keys

    @classmethod
    def _load_excluded_person_keys(cls, path: str) -> set[str]:
        if not path:
            return set()
        rows = cls._read_csv(path)
        keys: set[str] = set()
        for row in rows:
            linkedin_url = str(row.get("person_linkedin_url") or row.get("linkedin_url") or "").strip()
            email = str(row.get("person_email") or row.get("email") or "").strip().lower()
            company_domain = _parse_domain(str(row.get("company_domain") or row.get("source_company_domain") or ""))
            full_name = str(row.get("full_name") or "").strip()
            if not full_name:
                full_name = " ".join(
                    part for part in [str(row.get("first_name") or "").strip(), str(row.get("last_name") or "").strip()] if part
                ).strip()
            key = build_person_record_key(
                person_linkedin_url=linkedin_url,
                person_email=email,
                company_domain=company_domain,
                person_full_name=full_name,
            )
            if key:
                keys.add(key)
        return keys

    @staticmethod
    def _fetch_supabase_company_keys(company_rows: list[dict[str, str]]) -> set[str]:
        keys = {
            build_company_record_key(
                company_linkedin_url=str(row.get("linkedin_url") or "").strip(),
                company_domain=_parse_domain(str(row.get("resolved_domain") or "")),
                company_name=str(row.get("company_name") or "").strip(),
            )
            for row in company_rows
            if (row.get("tier") or "").strip() == FQHC_TIER
        }
        return fetch_existing_company_record_keys([key for key in keys if key])

    @staticmethod
    def _fetch_supabase_person_keys(people_rows: list[dict[str, str]]) -> set[str]:
        keys = {
            build_person_record_key(
                person_linkedin_url=str(row.get("person_linkedin_url") or "").strip(),
                person_email=str(row.get("person_email") or "").strip().lower(),
                company_domain=_parse_domain(str(row.get("source_company_domain") or "")),
                person_full_name=" ".join(
                    part
                    for part in [str(row.get("first_name") or "").strip(), str(row.get("last_name") or "").strip()]
                    if part
                ).strip(),
            )
            for row in people_rows
            if (row.get("source_company_tier") or "").strip() == FQHC_TIER
        }
        return fetch_existing_person_record_keys([key for key in keys if key])

    async def _qualify_companies(
        self,
        *,
        paths: RunPaths,
        companies: list[dict[str, Any]],
        posted_limit: str,
        include_company_posts: bool,
        budget_tracker: RunBudgetTracker,
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(6)

        async def worker(company: dict[str, Any]) -> dict[str, Any]:
            linkedin_url = str(company.get("linkedin_url") or "").strip()
            async with semaphore:
                response = await self._harvest.get_company(url=linkedin_url)
                budget_tracker.record_harvest_company_get(
                    stage="fqhc_company_qualifier",
                    company_url=linkedin_url,
                )
                append_jsonl(
                    paths.harvest_company_jsonl,
                    {
                        "provider": "harvest",
                        "stage": "fqhc_company_qualifier",
                        "company_url": linkedin_url,
                        "response": response.data,
                    },
                )
                posts_payload: dict[str, Any] | None = None
                if include_company_posts:
                    posts_response = await self._harvest.company_posts(
                        company=linkedin_url,
                        posted_limit=posted_limit,
                        page=1,
                    )
                    budget_tracker.record_harvest_company_posts(
                        stage="fqhc_company_posts",
                        company_url=linkedin_url,
                        page=1,
                    )
                    append_jsonl(
                        paths.harvest_company_jsonl,
                        {
                            "provider": "harvest",
                            "stage": "fqhc_company_posts",
                            "company_url": linkedin_url,
                            "response": posts_response.data,
                        },
                    )
                    if isinstance(posts_response.data, dict):
                        posts_payload = posts_response.data
                payload = response.data if isinstance(response.data, dict) else {}
                return self._build_company_qualifier_row(
                    source_row=company,
                    harvest_payload=payload,
                    posts_payload=posts_payload,
                )

        return await asyncio.gather(*(worker(company) for company in companies if company.get("linkedin_url")))

    async def _qualify_people(
        self,
        *,
        paths: RunPaths,
        people: list[dict[str, Any]],
        company_lookup: dict[str, dict[str, Any]],
        persona: str,
        posted_limit: str,
        include_profile_posts: bool,
        budget_tracker: RunBudgetTracker,
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(6)
        cached_events = _read_jsonl(paths.harvest_profile_jsonl)
        cached_payloads: dict[str, dict[str, Any]] = {}
        for event in cached_events:
            if event.get("stage") != "fqhc_profile_qualifier":
                continue
            linkedin_url = str(event.get("linkedin_url") or "").strip()
            response = event.get("response")
            if linkedin_url and isinstance(response, dict):
                cached_payloads[linkedin_url] = response

        async def worker(person: dict[str, Any]) -> dict[str, Any]:
            linkedin_url = str(person.get("person_linkedin_url") or "").strip()
            async with semaphore:
                try:
                    response = await self._harvest.get_profile(
                        url=linkedin_url,
                        include_about_profile=True,
                    )
                    budget_tracker.record_harvest_profile_get(
                        stage="fqhc_profile_qualifier",
                        linkedin_url=linkedin_url,
                        include_email=False,
                        use_main_profile=False,
                    )
                    append_jsonl(
                        paths.harvest_profile_jsonl,
                        {
                            "provider": "harvest",
                            "stage": "fqhc_profile_qualifier",
                            "linkedin_url": linkedin_url,
                            "company_name": person.get("source_company_name", ""),
                            "company_domain": person.get("source_company_domain", ""),
                            "response": response.data,
                        },
                    )
                    posts_payload: dict[str, Any] | None = None
                    if include_profile_posts:
                        posts_response = await self._harvest.profile_posts(
                            profile=linkedin_url,
                            posted_limit=posted_limit,
                            page=1,
                        )
                        budget_tracker.record_harvest_profile_posts(
                            stage="fqhc_profile_posts",
                            linkedin_url=linkedin_url,
                            page=1,
                        )
                        append_jsonl(
                            paths.harvest_profile_jsonl,
                            {
                                "provider": "harvest",
                                "stage": "fqhc_profile_posts",
                                "linkedin_url": linkedin_url,
                                "company_name": person.get("source_company_name", ""),
                                "company_domain": person.get("source_company_domain", ""),
                                "response": posts_response.data,
                            },
                        )
                        if isinstance(posts_response.data, dict):
                            posts_payload = posts_response.data
                    payload = response.data if isinstance(response.data, dict) else {}
                    company_row = company_lookup.get(person.get("source_company_slug") or "", {})
                    return self._build_people_qualifier_row(
                        source_row=person,
                        company_row=company_row,
                        harvest_payload=payload,
                        posts_payload=posts_payload,
                        persona=persona,
                    )
                except Exception as exc:  # noqa: BLE001
                    append_jsonl(
                        paths.harvest_profile_jsonl,
                        {
                            "provider": "harvest",
                            "stage": "fqhc_profile_qualifier_error",
                            "linkedin_url": linkedin_url,
                            "company_name": person.get("source_company_name", ""),
                            "company_domain": person.get("source_company_domain", ""),
                            "error": str(exc),
                        },
                    )
                    return self._build_people_error_row(
                        source_row=person,
                        persona=persona,
                        error=str(exc),
                    )

        uncached_people = [
            person
            for person in people
            if person.get("person_linkedin_url")
            and str(person.get("person_linkedin_url")).strip() not in cached_payloads
        ]
        new_rows = await asyncio.gather(*(worker(person) for person in uncached_people))
        new_rows_by_url = {
            str(row.get("person_linkedin_url") or "").strip(): row
            for row in new_rows
            if row.get("person_linkedin_url")
        }
        ordered_rows: list[dict[str, Any]] = []
        for person in people:
            linkedin_url = str(person.get("person_linkedin_url") or "").strip()
            if not linkedin_url:
                continue
            if linkedin_url in cached_payloads:
                company_row = company_lookup.get(person.get("source_company_slug") or "", {})
                ordered_rows.append(
                    self._build_people_qualifier_row(
                        source_row=person,
                        company_row=company_row,
                        harvest_payload=cached_payloads[linkedin_url],
                        posts_payload=None,
                        persona=persona,
                    )
                )
                continue
            row = new_rows_by_url.get(linkedin_url)
            if row is not None:
                ordered_rows.append(row)
        return ordered_rows

    @staticmethod
    def _build_company_qualifier_row(
        *,
        source_row: dict[str, Any],
        harvest_payload: dict[str, Any],
        posts_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        element = harvest_payload.get("element")
        if not isinstance(element, dict):
            element = {}
        harvest_name = str(element.get("name") or "").strip()
        harvest_website = str(element.get("website") or "").strip()
        harvest_domain = _parse_domain(harvest_website)
        harvest_description = str(
            element.get("description") or element.get("tagline") or ""
        ).strip()
        company_name = str(source_row.get("company_name") or "").strip()
        healthcare_keywords = ("health", "clinic", "medical", "fqhc", "community")
        healthcare_match = any(
            keyword in _normalize_text(f"{company_name} {harvest_description}")
            for keyword in healthcare_keywords
        )
        identity_match = _company_match(company_name, harvest_name or company_name)
        qualifies = identity_match and bool(harvest_name)
        return {
            "linkedin_slug": source_row.get("linkedin_slug", ""),
            "linkedin_url": source_row.get("linkedin_url", ""),
            "company_name": company_name,
            "tier": source_row.get("tier", ""),
            "tier_label": source_row.get("tier_label", ""),
            "primary_keyword": source_row.get("primary_keyword", ""),
            "size_bracket": source_row.get("size_bracket", ""),
            "resolved_domain": source_row.get("resolved_domain", ""),
            "resolve_status": source_row.get("resolve_status", ""),
            "matching_people_count": source_row.get("matching_people_count", ""),
            "harvest_company_name": harvest_name,
            "harvest_company_domain": harvest_domain,
            "harvest_company_description": harvest_description,
            "harvest_employee_count": str(element.get("employeeCount") or "").strip(),
            "harvest_employee_count_range": _extract_company_headcount_range(harvest_payload),
            "harvest_status": str(harvest_payload.get("status") or "").strip(),
            "company_identity_match": "true" if identity_match else "false",
            "company_healthcare_keyword_match": "true" if healthcare_match else "false",
            "company_soft_qualifies": "true" if qualifies else "false",
            "company_soft_qualifier_reason": (
                "harvest identity matched company and returned a live company object"
                if qualifies
                else "review harvest company identity before deep research"
            ),
            "company_recent_posts_summary": _build_recent_posts_summary(posts_payload or {}),
        }

    @staticmethod
    def _build_people_qualifier_row(
        *,
        source_row: dict[str, Any],
        company_row: dict[str, Any],
        harvest_payload: dict[str, Any],
        posts_payload: dict[str, Any] | None,
        persona: str,
    ) -> dict[str, Any]:
        element = harvest_payload.get("element")
        if not isinstance(element, dict):
            element = {}
        harvest_full_name = " ".join(
            part
            for part in [
                str(element.get("firstName") or "").strip(),
                str(element.get("lastName") or "").strip(),
            ]
            if part
        ).strip()
        harvest_headline = str(element.get("headline") or "").strip()
        harvest_about = str(element.get("about") or "").strip()
        harvest_current_company = _extract_current_company(harvest_payload)
        expected_company = str(source_row.get("source_company_name") or "").strip()
        current_company_match = _company_match(expected_company, harvest_current_company or expected_company)
        title_soft_match = _title_matches_persona(harvest_headline or str(source_row.get("parsed_title") or ""), persona)
        final_match = current_company_match and title_soft_match
        location = element.get("location")
        harvest_location = ""
        if isinstance(location, dict):
            harvest_location = str(location.get("linkedinText") or "").strip()
        return {
            "person_slug": source_row.get("person_slug", ""),
            "person_linkedin_url": source_row.get("person_linkedin_url", ""),
            "first_name": source_row.get("first_name", ""),
            "last_name": source_row.get("last_name", ""),
            "full_name": harvest_full_name or f"{source_row.get('first_name', '')} {source_row.get('last_name', '')}".strip(),
            "source_company_slug": source_row.get("source_company_slug", ""),
            "source_company_name": expected_company,
            "source_company_domain": source_row.get("source_company_domain", "") or company_row.get("harvest_company_domain", ""),
            "source_company_tier": source_row.get("source_company_tier", ""),
            "source_company_size": source_row.get("source_company_size", ""),
            "priority_tier": source_row.get("priority_tier", ""),
            "role_group_label": source_row.get("role_group_label", ""),
            "selection_bucket": source_row.get("selection_bucket", ""),
            "selection_priority": source_row.get("selection_priority", ""),
            "corpus_parsed_title": source_row.get("parsed_title", ""),
            "rubric_persona": persona,
            "harvest_headline": harvest_headline,
            "harvest_about": harvest_about,
            "harvest_current_company": harvest_current_company,
            "harvest_location": harvest_location,
            "harvest_skills": _extract_profile_skills(harvest_payload),
            "harvest_recent_posts_summary": _build_recent_posts_summary(posts_payload or {}),
            "harvest_status": str(harvest_payload.get("status") or "").strip(),
            "current_company_match": "true" if current_company_match else "false",
            "title_soft_match": "true" if title_soft_match else "false",
            "person_soft_qualifies": "true" if final_match else "false",
            "person_soft_qualifier_reason": (
                "harvest profile confirms current employer and rubric-matching title"
                if final_match
                else "review title or current employer before writing copy"
            ),
        }

    @staticmethod
    def _build_people_error_row(
        *,
        source_row: dict[str, Any],
        persona: str,
        error: str,
    ) -> dict[str, Any]:
        return {
            "person_slug": source_row.get("person_slug", ""),
            "person_linkedin_url": source_row.get("person_linkedin_url", ""),
            "first_name": source_row.get("first_name", ""),
            "last_name": source_row.get("last_name", ""),
            "full_name": f"{source_row.get('first_name', '')} {source_row.get('last_name', '')}".strip(),
            "source_company_slug": source_row.get("source_company_slug", ""),
            "source_company_name": source_row.get("source_company_name", ""),
            "source_company_domain": source_row.get("source_company_domain", ""),
            "source_company_tier": source_row.get("source_company_tier", ""),
            "source_company_size": source_row.get("source_company_size", ""),
            "priority_tier": source_row.get("priority_tier", ""),
            "role_group_label": source_row.get("role_group_label", ""),
            "selection_bucket": source_row.get("selection_bucket", ""),
            "selection_priority": source_row.get("selection_priority", ""),
            "corpus_parsed_title": source_row.get("parsed_title", ""),
            "rubric_persona": persona,
            "harvest_headline": "",
            "harvest_about": "",
            "harvest_current_company": "",
            "harvest_location": "",
            "harvest_skills": "",
            "harvest_recent_posts_summary": "",
            "harvest_status": f"error: {error}",
            "current_company_match": "false",
            "title_soft_match": "false",
            "person_soft_qualifies": "false",
            "person_soft_qualifier_reason": error,
        }
