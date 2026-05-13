from __future__ import annotations

import csv
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
import re
from typing import Any

from listbuild.datasets import (
    CompanyRow,
    PersonRow,
    company_quality_score,
    person_quality_score,
    write_company_rows_csv,
    write_person_rows_csv,
)
from listbuild.normalize import normalize_url
from listbuild.runs import RunNaming, RunPaths, scaffold_run_directories
from listbuild.storage import sync_run_to_supabase


COMPANY_HEADER_ALIASES = {
    "company_name": ("company_name", "name", "company", "account_name"),
    "company_domain": ("company_domain", "domain", "website", "company_website"),
    "company_linkedin_url": (
        "company_linkedin_url",
        "linkedin_company_url",
        "linkedin_url",
        "company_linkedin",
    ),
    "company_description": (
        "company_description",
        "description",
        "summary",
        "company_summary",
    ),
    "company_offer": ("company_offer", "offer", "service_offer", "services"),
    "company_icp": ("company_icp", "icp", "ideal_customer_profile", "target_customer"),
    "company_painpoint": ("company_painpoint", "painpoint", "pain_point"),
    "company_size": ("company_size", "size", "headcount", "employee_count"),
    "company_headcount_exact": (
        "company_headcount_exact",
        "headcount_exact",
        "employee_count_exact",
    ),
    "company_headcount_range": (
        "company_headcount_range",
        "headcount_range",
        "employee_count_range",
    ),
    "company_signals": ("company_signals", "signals", "signal_summary"),
    "company_signal_sources": (
        "company_signal_sources",
        "signal_sources",
        "sources",
    ),
}

PEOPLE_HEADER_ALIASES = {
    "full_name": ("full_name", "person_full_name", "name", "contact_name"),
    "first_name": ("first_name", "person_first_name", "firstname"),
    "last_name": ("last_name", "person_last_name", "lastname"),
    "role": ("role", "person_title", "title", "job_title", "headline"),
    "role_description": (
        "role_description",
        "person_about",
        "about",
        "bio",
        "summary",
    ),
    "person_linkedin_url": (
        "person_linkedin_url",
        "linkedin_url",
        "profile_url",
        "linkedin_profile_url",
    ),
    "person_email": ("person_email", "email", "work_email"),
    "person_email_status": (
        "person_email_status",
        "email_status",
        "validation_status",
    ),
    "company_name": ("company_name", "company", "account_name"),
    "company_domain": ("company_domain", "domain", "company_website", "website"),
    "e1": ("e1",),
    "e2": ("e2",),
    "e3": ("e3",),
    "e4": ("e4",),
    "s1": ("s1",),
    "s2": ("s2",),
    "s3": ("s3",),
    "s4": ("s4",),
    "p1": ("p1",),
    "p2": ("p2",),
    "p3": ("p3",),
    "p4": ("p4",),
}


@dataclass(frozen=True, slots=True)
class ImportRunResult:
    run_slug: str
    rows_written: int
    output_csv: str
    supabase: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "rows_written": self.rows_written,
            "output_csv": self.output_csv,
            "supabase": self.supabase,
        }


def _normalize_header(value: str) -> str:
    lowered = value.strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [{_normalize_header(key): (value or "") for key, value in row.items()} for row in rows]


def _lookup_value(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = row.get(_normalize_header(alias), "")
        if value and value.strip():
            return " ".join(value.split())
    return ""


def _normalize_domain(value: str) -> str:
    if not value.strip():
        return ""
    normalized = normalize_url(value)
    if normalized is not None:
        return normalized.host
    return value.strip().lower().removeprefix("www.")


def _normalize_linkedin_url(value: str) -> str:
    if not value.strip():
        return ""
    normalized = normalize_url(value)
    if normalized is not None:
        return normalized.normalized_url.rstrip("/")
    return value.strip().rstrip("/")


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in full_name.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _email_syntax_status(email: str) -> str:
    candidate = email.strip().lower()
    if not candidate:
        return ""
    _, parsed = parseaddr(candidate)
    if not parsed or "@" not in parsed:
        return "invalid_syntax"
    local, _, domain = parsed.partition("@")
    if not local or not domain or "." not in domain:
        return "invalid_syntax"
    return "valid_syntax"


def import_companies_csv(
    *,
    input_path: str | Path,
    paths: RunPaths,
    source_query: str = "",
    upsert_supabase: bool = False,
) -> ImportRunResult:
    scaffold_run_directories(paths)
    source_rows = _read_csv(input_path)
    rows: list[CompanyRow] = []
    for source_row in source_rows:
        row = CompanyRow(
            run_slug=paths.naming.run_slug,
            seller_slug=paths.naming.seller_slug,
            segment_slug=paths.naming.segment_slug,
            source_query=source_query,
            company_name=_lookup_value(source_row, COMPANY_HEADER_ALIASES["company_name"]),
            company_domain=_normalize_domain(
                _lookup_value(source_row, COMPANY_HEADER_ALIASES["company_domain"])
            ),
            company_linkedin_url=_normalize_linkedin_url(
                _lookup_value(
                    source_row,
                    COMPANY_HEADER_ALIASES["company_linkedin_url"],
                )
            ),
            company_description=_lookup_value(
                source_row,
                COMPANY_HEADER_ALIASES["company_description"],
            ),
            company_offer=_lookup_value(source_row, COMPANY_HEADER_ALIASES["company_offer"]),
            company_icp=_lookup_value(source_row, COMPANY_HEADER_ALIASES["company_icp"]),
            company_painpoint=_lookup_value(
                source_row,
                COMPANY_HEADER_ALIASES["company_painpoint"],
            ),
            company_size=_lookup_value(source_row, COMPANY_HEADER_ALIASES["company_size"]),
            company_headcount_exact=_lookup_value(
                source_row,
                COMPANY_HEADER_ALIASES["company_headcount_exact"],
            ),
            company_headcount_range=_lookup_value(
                source_row,
                COMPANY_HEADER_ALIASES["company_headcount_range"],
            ),
            company_signals=_lookup_value(source_row, COMPANY_HEADER_ALIASES["company_signals"]),
            company_signal_sources=_lookup_value(
                source_row,
                COMPANY_HEADER_ALIASES["company_signal_sources"],
            ),
            company_quality_score="0",
            company_needs_followup="true",
            harvest_company_id="",
            harvest_status="imported",
        )
        score = company_quality_score(row)
        rows.append(
            CompanyRow(
                **{
                    **row.to_dict(),
                    "company_quality_score": str(score),
                    "company_needs_followup": "true" if score < 70 else "false",
                }
            )
        )

    write_company_rows_csv(rows, paths.company_csv)
    supabase_result = None
    if upsert_supabase:
        supabase_result = sync_run_to_supabase(
            paths=paths,
            metadata={
                "workflow": "import-companies-csv",
                "source_path": str(input_path),
                "source_query": source_query,
            },
        )
    return ImportRunResult(
        run_slug=paths.naming.run_slug,
        rows_written=len(rows),
        output_csv=str(paths.company_csv),
        supabase=supabase_result,
    )


def import_people_csv(
    *,
    input_path: str | Path,
    paths: RunPaths,
    source_role_segment: str = "",
    upsert_supabase: bool = False,
) -> ImportRunResult:
    scaffold_run_directories(paths)
    source_rows = _read_csv(input_path)
    rows: list[PersonRow] = []
    for source_row in source_rows:
        company_name = _lookup_value(source_row, PEOPLE_HEADER_ALIASES["company_name"])
        company_domain = _normalize_domain(
            _lookup_value(source_row, PEOPLE_HEADER_ALIASES["company_domain"])
        )
        full_name = _lookup_value(source_row, PEOPLE_HEADER_ALIASES["full_name"])
        first_name = _lookup_value(source_row, PEOPLE_HEADER_ALIASES["first_name"])
        last_name = _lookup_value(source_row, PEOPLE_HEADER_ALIASES["last_name"])
        if full_name and (not first_name and not last_name):
            first_name, last_name = _split_full_name(full_name)
        if not full_name:
            full_name = " ".join(part for part in [first_name, last_name] if part).strip()
        email = _lookup_value(source_row, PEOPLE_HEADER_ALIASES["person_email"]).lower()
        email_status = _lookup_value(
            source_row,
            PEOPLE_HEADER_ALIASES["person_email_status"],
        ) or _email_syntax_status(email)

        row = PersonRow(
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            company_name=company_name,
            role=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["role"]),
            role_description=_lookup_value(
                source_row,
                PEOPLE_HEADER_ALIASES["role_description"],
            ),
            e1=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["e1"]),
            e2=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["e2"]),
            e3=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["e3"]),
            e4=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["e4"]),
            s1=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["s1"]),
            s2=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["s2"]),
            s3=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["s3"]),
            s4=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["s4"]),
            p1=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["p1"]),
            p2=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["p2"]),
            p3=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["p3"]),
            p4=_lookup_value(source_row, PEOPLE_HEADER_ALIASES["p4"]),
            run_slug=paths.naming.run_slug,
            seller_slug=paths.naming.seller_slug,
            segment_slug=paths.naming.segment_slug,
            source_role_segment=source_role_segment
            or _lookup_value(source_row, ("source_role_segment", "role_segment", "persona")),
            company_domain=company_domain,
            person_linkedin_url=_normalize_linkedin_url(
                _lookup_value(source_row, PEOPLE_HEADER_ALIASES["person_linkedin_url"])
            ),
            person_email=email,
            person_email_status=email_status,
            person_quality_score="0",
            harvest_profile_id="",
            harvest_status="imported",
        )
        rows.append(
            PersonRow(
                **{
                    **row.to_storage_dict(),
                    "person_quality_score": str(person_quality_score(row)),
                }
            )
        )

    write_person_rows_csv(rows, paths.people_csv)
    supabase_result = None
    if upsert_supabase:
        supabase_result = sync_run_to_supabase(
            paths=paths,
            metadata={
                "workflow": "import-people-csv",
                "source_path": str(input_path),
                "source_role_segment": source_role_segment,
            },
            people_rows_override=[row.to_storage_dict() for row in rows],
        )
    return ImportRunResult(
        run_slug=paths.naming.run_slug,
        rows_written=len(rows),
        output_csv=str(paths.people_csv),
        supabase=supabase_result,
    )
