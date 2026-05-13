from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from listbuild.normalize import normalize_url
from listbuild.runs import RunNaming


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def _read_json_records(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if target.is_dir():
        records: list[dict[str, Any]] = []
        for file_path in sorted(target.glob("*.json")):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                records.append(payload)
        return records

    if target.suffix == ".jsonl":
        records = []
        for line in target.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                records.append(payload)
        return records

    payload = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"Unsupported JSON payload in {target}")


def _unwrap_provider_payload(record: dict[str, Any]) -> dict[str, Any]:
    response = record.get("response")
    if isinstance(response, dict):
        return response
    return record


def _record_stage(record: dict[str, Any]) -> str:
    return _clean_text(record.get("stage")).lower()


def _headcount_range_text(payload: dict[str, Any]) -> str:
    employee_count_range = payload.get("employeeCountRange")
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


def _parse_company_domain(raw_value: str) -> str:
    normalized = normalize_url(raw_value)
    if normalized is None:
        return raw_value.strip().lower()
    return normalized.host


def _normalize_company_name_key(value: str) -> str:
    return _clean_text(value).casefold()


@dataclass(frozen=True, slots=True)
class CompanyRow:
    run_slug: str
    seller_slug: str
    segment_slug: str
    source_query: str
    company_name: str
    company_domain: str
    company_linkedin_url: str
    company_description: str
    company_offer: str
    company_icp: str
    company_painpoint: str
    company_size: str
    company_headcount_exact: str
    company_headcount_range: str
    company_signals: str
    company_signal_sources: str
    company_recent_post_summary: str
    company_recent_post_urls: str
    company_research_page_count: str
    company_research_page_urls: str
    company_research_crawler_provider: str
    company_quality_score: str
    company_needs_followup: str
    harvest_company_id: str
    harvest_status: str

    @classmethod
    def headers(cls) -> list[str]:
        return list(cls.__dataclass_fields__.keys())

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PersonRow:
    full_name: str
    first_name: str
    last_name: str
    company_name: str
    role: str
    role_description: str
    e1: str
    e2: str
    e3: str
    e4: str
    s1: str
    s2: str
    s3: str
    s4: str
    p1: str
    p2: str
    p3: str
    p4: str
    run_slug: str
    seller_slug: str
    segment_slug: str
    source_role_segment: str
    company_domain: str
    person_linkedin_url: str
    person_email: str
    person_email_status: str
    person_quality_score: str
    harvest_profile_id: str
    harvest_status: str

    EXPORT_HEADERS = [
        "full_name",
        "first_name",
        "last_name",
        "company_name",
        "role",
        "role_title",
        "role_description",
        "e1",
        "e2",
        "e3",
        "e4",
        "s1",
        "s2",
        "s3",
        "s4",
        "p1",
        "p2",
        "p3",
        "p4",
    ]

    @classmethod
    def headers(cls) -> list[str]:
        return list(cls.EXPORT_HEADERS)

    def to_dict(self) -> dict[str, str]:
        payload = asdict(self)
        payload["role_title"] = payload.get("role", "")
        return {header: str(payload.get(header, "")) for header in self.EXPORT_HEADERS}

    def to_storage_dict(self) -> dict[str, str]:
        payload = asdict(self)
        return {key: "" if value is None else str(value) for key, value in payload.items()}


def company_quality_score(row: CompanyRow) -> int:
    score = 0
    if row.company_name:
        score += 10
    if row.company_domain:
        score += 15
    if row.company_description:
        score += 15
    if row.company_size or row.company_headcount_exact or row.company_headcount_range:
        score += 10
    if row.company_offer:
        score += 15
    if row.company_icp:
        score += 15
    if row.company_painpoint:
        score += 10
    if row.company_signals:
        score += 10
    return min(score, 100)


def person_quality_score(row: PersonRow) -> int:
    score = 0
    if row.full_name:
        score += 15
    if row.person_linkedin_url:
        score += 15
    if row.role:
        score += 15
    if row.company_name:
        score += 15
    if row.role_description:
        score += 10
    if row.person_email:
        score += 10
    return min(score, 100)


def company_row_from_harvest(
    payload: dict[str, Any],
    *,
    naming: RunNaming,
    source_query: str = "",
) -> CompanyRow:
    element = payload.get("element", {})
    if not isinstance(element, dict):
        element = {}
    website = _clean_text(element.get("website"))
    company_domain = _parse_company_domain(website) if website else ""
    description = _clean_text(element.get("description")) or _clean_text(
        element.get("tagline")
    )
    headcount_exact = _clean_text(element.get("employeeCount"))
    headcount_range = _headcount_range_text(element)
    size = headcount_exact or headcount_range

    row = CompanyRow(
        run_slug=naming.run_slug,
        seller_slug=naming.seller_slug,
        segment_slug=naming.segment_slug,
        source_query=source_query,
        company_name=_clean_text(element.get("name")),
        company_domain=company_domain,
        company_linkedin_url=_clean_text(element.get("linkedinUrl")),
        company_description=description,
        company_offer="",
        company_icp="",
        company_painpoint="",
        company_size=size,
        company_headcount_exact=headcount_exact,
        company_headcount_range=headcount_range,
        company_signals="",
        company_signal_sources="",
        company_recent_post_summary="",
        company_recent_post_urls="",
        company_research_page_count="0",
        company_research_page_urls="",
        company_research_crawler_provider="",
        company_quality_score="0",
        company_needs_followup="true",
        harvest_company_id=_clean_text(element.get("id")),
        harvest_status=_clean_text(payload.get("status")),
    )
    score = company_quality_score(row)
    needs_followup = "true" if score < 70 else "false"
    return CompanyRow(
        **{
            **row.to_dict(),
            "company_quality_score": str(score),
            "company_needs_followup": needs_followup,
        }
    )


def _join_skills(skills: Any) -> str:
    if not isinstance(skills, list):
        return ""
    values: list[str] = []
    for item in skills:
        if isinstance(item, dict):
            name = _clean_text(item.get("name"))
            if name:
                values.append(name)
    return " | ".join(values[:10])


def _build_recent_posts_summary(payload: dict[str, Any]) -> str:
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return ""
    snippets: list[str] = []
    for item in elements[:3]:
        if not isinstance(item, dict):
            continue
        content = _clean_text(item.get("content"))
        if content:
            snippets.append(content[:240])
    return " || ".join(snippets)


def person_row_from_harvest_profile(
    payload: dict[str, Any],
    *,
    naming: RunNaming,
    company_name: str = "",
    company_domain: str = "",
    source_role_segment: str = "",
    posts_payload: dict[str, Any] | None = None,
) -> PersonRow:
    element = payload.get("element", {})
    if not isinstance(element, dict):
        element = {}

    current_position = element.get("currentPosition")
    current_company = company_name
    if isinstance(current_position, list) and current_position:
        first_position = current_position[0]
        if isinstance(first_position, dict):
            current_company = _clean_text(first_position.get("companyName")) or company_name

    first_name = _clean_text(element.get("firstName"))
    last_name = _clean_text(element.get("lastName"))
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    location = element.get("location")
    location_text = ""
    if isinstance(location, dict):
        location_text = _clean_text(location.get("linkedinText"))

    signal_parts = [
        _clean_text(element.get("headline")),
        _join_skills(element.get("skills")),
    ]
    signal_summary = " | ".join(part for part in signal_parts if part)
    recent_posts_summary = ""
    if posts_payload is not None:
        recent_posts_summary = _build_recent_posts_summary(posts_payload)
    role_description = _clean_text(element.get("about")) or recent_posts_summary or signal_summary

    row = PersonRow(
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        role=_clean_text(element.get("headline")),
        role_description=role_description,
        e1="",
        e2="",
        e3="",
        e4="",
        s1="",
        s2="",
        s3="",
        s4="",
        p1="",
        p2="",
        p3="",
        p4="",
        run_slug=naming.run_slug,
        seller_slug=naming.seller_slug,
        segment_slug=naming.segment_slug,
        source_role_segment=source_role_segment,
        company_domain=company_domain,
        person_linkedin_url=_clean_text(element.get("linkedinUrl")),
        person_email="",
        person_email_status="",
        person_quality_score="0",
        harvest_profile_id=_clean_text(element.get("id")),
        harvest_status=_clean_text(payload.get("status")),
    )
    return PersonRow(
        **{
            **row.to_storage_dict(),
            "person_quality_score": str(person_quality_score(row)),
        }
    )


def _company_row_key(row: CompanyRow) -> str:
    return (
        row.company_linkedin_url
        or row.harvest_company_id
        or row.company_domain
        or row.company_name
    )


def _profile_payload_key(payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    if metadata:
        for key in ("linkedin_url", "profile", "profile_url"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    element = payload.get("element")
    if isinstance(element, dict):
        for key in ("linkedinUrl", "publicIdentifier", "id"):
            value = element.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _materialize_company_rows(
    *,
    records: list[dict[str, Any]],
    naming: RunNaming,
    source_query: str,
) -> list[CompanyRow]:
    rows_by_key: dict[str, CompanyRow] = {}
    for record in records:
        stage = _record_stage(record)
        if stage and stage != "company_get":
            continue
        payload = _unwrap_provider_payload(record)
        if not isinstance(payload, dict):
            continue
        row = company_row_from_harvest(
            payload,
            naming=naming,
            source_query=source_query,
        )
        if not row.company_linkedin_url:
            company_url = _clean_text(record.get("company_url"))
            if company_url:
                row = CompanyRow(
                    **{
                        **row.to_dict(),
                        "company_linkedin_url": company_url,
                    }
                )
        key = _company_row_key(row)
        if not key:
            continue
        rows_by_key[key] = row
    return list(rows_by_key.values())


def _materialize_person_rows(
    *,
    records: list[dict[str, Any]],
    naming: RunNaming,
    company_name: str,
    company_domain: str,
    company_domains_by_name: dict[str, str] | None = None,
    source_role_segment: str,
) -> list[PersonRow]:
    profile_records: dict[str, dict[str, Any]] = {}
    posts_by_key: dict[str, dict[str, Any]] = {}
    profile_meta_by_key: dict[str, dict[str, str]] = {}

    for record in records:
        stage = _record_stage(record)
        payload = _unwrap_provider_payload(record)
        if not isinstance(payload, dict):
            continue

        if stage and stage not in {"profile_get", "profile_posts"}:
            continue

        key = _profile_payload_key(payload, record)
        if not key:
            continue

        if stage == "profile_posts":
            posts_by_key[key] = payload
            continue

        profile_records[key] = payload
        profile_meta_by_key[key] = {
            "company_name": _clean_text(record.get("company_name")),
            "company_domain": _parse_company_domain(_clean_text(record.get("company_domain"))),
            "role_segment": _clean_text(record.get("role_segment")),
        }

    rows: list[PersonRow] = []
    for key, payload in profile_records.items():
        meta = profile_meta_by_key.get(key, {})
        resolved_company_name = meta.get("company_name") or company_name
        resolved_company_domain = meta.get("company_domain") or company_domain
        if not resolved_company_domain and company_domains_by_name:
            resolved_company_domain = company_domains_by_name.get(
                _normalize_company_name_key(resolved_company_name),
                "",
            )
        resolved_role_segment = meta.get("role_segment") or source_role_segment
        rows.append(
            person_row_from_harvest_profile(
                payload,
                naming=naming,
                company_name=resolved_company_name,
                company_domain=resolved_company_domain,
                source_role_segment=resolved_role_segment,
                posts_payload=posts_by_key.get(key),
            )
        )
    return rows


def write_company_rows_csv(rows: Iterable[CompanyRow], output_path: str | Path) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    row_list = list(rows)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CompanyRow.headers())
        writer.writeheader()
        for row in row_list:
            writer.writerow(row.to_dict())


def write_person_rows_csv(rows: Iterable[PersonRow], output_path: str | Path) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    row_list = list(rows)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PersonRow.headers())
        writer.writeheader()
        for row in row_list:
            writer.writerow(row.to_dict())


def materialize_harvest_companies_to_csv(
    *,
    input_path: str | Path,
    output_path: str | Path,
    naming: RunNaming,
    source_query: str = "",
) -> int:
    records = _read_json_records(input_path)
    rows = _materialize_company_rows(
        records=records,
        naming=naming,
        source_query=source_query,
    )
    write_company_rows_csv(rows, output_path)
    return len(rows)


def materialize_harvest_profiles_to_csv(
    *,
    input_path: str | Path,
    output_path: str | Path,
    naming: RunNaming,
    company_name: str = "",
    company_domain: str = "",
    source_role_segment: str = "",
) -> int:
    rows = load_harvest_profile_rows(
        input_path=input_path,
        naming=naming,
        company_name=company_name,
        company_domain=company_domain,
        source_role_segment=source_role_segment,
    )
    write_person_rows_csv(rows, output_path)
    return len(rows)


def load_harvest_profile_rows(
    *,
    input_path: str | Path,
    naming: RunNaming,
    company_name: str = "",
    company_domain: str = "",
    company_domains_by_name: dict[str, str] | None = None,
    source_role_segment: str = "",
) -> list[PersonRow]:
    records = _read_json_records(input_path)
    return _materialize_person_rows(
        records=records,
        naming=naming,
        company_name=company_name,
        company_domain=company_domain,
        company_domains_by_name=company_domains_by_name,
        source_role_segment=source_role_segment,
    )
