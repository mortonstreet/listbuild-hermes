#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

from listbuild.config import load_local_env, load_settings
from listbuild.providers import ProspeoClient


def utc_stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_company_website(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = text.removeprefix("https://").removeprefix("http://")
    text = text.removeprefix("www.")
    return text.strip("/")


def normalize_company_name(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(
        r"\b(incorporated|inc|llc|ltd|limited|corp|corporation|co|company|group|holdings)\b",
        " ",
        text,
    )
    return " ".join(text.split())


def read_rows(path: Path) -> list[dict[str, str]]:
    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            break
        except OverflowError:
            field_limit //= 10
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_identifier(index: int, row: dict[str, str]) -> str:
    full_name = (row.get("full_name") or "").strip().lower().replace(" ", "_")
    company = (row.get("company_name") or "").strip().lower().replace(" ", "_")
    return f"{index:04d}:{full_name}:{company}"


def prospeo_payload_record(index: int, row: dict[str, str]) -> dict[str, str]:
    payload: dict[str, str] = {"identifier": build_identifier(index, row)}
    linkedin_url = (row.get("person_linkedin_url") or row.get("linkedin_profile_url") or "").strip()
    full_name = (row.get("full_name") or "").strip()
    first_name = (row.get("first_name") or "").strip()
    last_name = (row.get("last_name") or "").strip()
    company_name = (row.get("company_name") or "").strip()
    company_website = normalize_company_website(row.get("company_domain") or "")
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    if full_name:
        payload["full_name"] = full_name
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if company_name:
        payload["company_name"] = company_name
    if company_website:
        payload["company_website"] = company_website
    return payload


def prospeo_email_fields(match: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    person = match.get("person") if isinstance(match.get("person"), dict) else {}
    email = person.get("email") if isinstance(person.get("email"), dict) else {}
    address = str(email.get("email") or "").strip().lower()
    status = str(email.get("status") or "").strip().upper()
    return address, status, email


def prospeo_company_fields(match: dict[str, Any]) -> tuple[str, str]:
    company = match.get("company") if isinstance(match.get("company"), dict) else {}
    name = str(company.get("name") or "").strip()
    domain = normalize_company_website(
        str(company.get("domain") or company.get("website") or "").strip()
    )
    return name, domain


def response_identifier_set(items: list[Any]) -> set[str]:
    identifiers: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            identifier = str(item.get("identifier") or "").strip()
            if identifier:
                identifiers.add(identifier)
            continue
        identifier = str(item or "").strip()
        if identifier:
            identifiers.add(identifier)
    return identifiers


def company_match(row: dict[str, str], match: dict[str, Any]) -> tuple[bool, str, str]:
    target_domain = normalize_company_website(row.get("company_domain") or "")
    target_name = normalize_company_name(row.get("company_name") or "")
    prospeo_name, prospeo_domain = prospeo_company_fields(match)
    normalized_prospeo_name = normalize_company_name(prospeo_name)

    matched = False
    if target_domain and prospeo_domain and target_domain == prospeo_domain:
        matched = True
    elif target_name and normalized_prospeo_name and target_name == normalized_prospeo_name:
        matched = True
    return matched, prospeo_name, prospeo_domain


async def enrich(
    *,
    input_csv: Path,
    output_csv: Path,
    raw_jsonl: Path,
    summary_json: Path,
    only_verified_email: bool,
    batch_size: int,
    max_rows: int,
) -> None:
    load_local_env()
    settings = load_settings()

    rows = read_rows(input_csv)
    if max_rows > 0:
        rows = rows[:max_rows]

    fieldnames = list(rows[0].keys()) if rows else []
    for extra in ("prospeo_email_status", "prospeo_free_enrichment", "prospeo_person_id"):
        if extra not in fieldnames:
            fieldnames.append(extra)
    for extra in ("prospeo_company_name", "prospeo_company_domain", "prospeo_company_match"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    pending_indices: list[int] = []
    pending_records: list[dict[str, str]] = []
    for idx, row in enumerate(rows):
        if (row.get("person_email") or "").strip():
            continue
        if not (row.get("person_linkedin_url") or row.get("linkedin_profile_url") or "").strip():
            continue
        pending_indices.append(idx)
        pending_records.append(prospeo_payload_record(idx, row))

    started_at = utc_stamp()
    matched = 0
    company_matched = 0
    not_matched = 0
    invalid = 0
    total_batches = 0
    total_cost = 0

    async with ProspeoClient(settings.prospeo) as client:
        for offset in range(0, len(pending_records), batch_size):
            batch = pending_records[offset : offset + batch_size]
            total_batches += 1
            payload = {
                "only_verified_email": only_verified_email,
                "data": batch,
            }
            response = await client.raw(
                "POST",
                "/bulk-enrich-person",
                payload=payload,
                category="enrich",
            )
            data = response.data if isinstance(response.data, dict) else {}
            append_jsonl(
                raw_jsonl,
                {
                    "logged_at": utc_stamp(),
                    "batch_number": total_batches,
                    "batch_size": len(batch),
                    "payload": payload,
                    "response": data,
                    "status_code": response.status_code,
                },
            )
            total_cost += int(data.get("response", {}).get("total_cost") or data.get("total_cost") or 0)
            matched_items = data.get("response", {}).get("matched") if isinstance(data.get("response"), dict) else data.get("matched")
            if not isinstance(matched_items, list):
                matched_items = []
            not_matched_items = data.get("response", {}).get("not_matched") if isinstance(data.get("response"), dict) else data.get("not_matched")
            if not isinstance(not_matched_items, list):
                not_matched_items = []
            invalid_items = data.get("response", {}).get("invalid_datapoints") if isinstance(data.get("response"), dict) else data.get("invalid_datapoints")
            if not isinstance(invalid_items, list):
                invalid_items = []

            match_lookup = {
                str(item.get("identifier") or ""): item
                for item in matched_items
                if isinstance(item, dict)
            }
            not_matched_lookup = response_identifier_set(not_matched_items)
            invalid_lookup = response_identifier_set(invalid_items)
            matched += len(match_lookup)
            not_matched += len(not_matched_lookup)
            invalid += len(invalid_lookup)

            for record in batch:
                identifier = record["identifier"]
                row_index = int(identifier.split(":", 1)[0])
                row = rows[row_index]
                item = match_lookup.get(identifier)
                if item is None:
                    if identifier in invalid_lookup:
                        row["person_email_status"] = row.get("person_email_status") or "prospeo_invalid_datapoints"
                    elif identifier in not_matched_lookup:
                        row["person_email_status"] = row.get("person_email_status") or "prospeo_no_match"
                    continue
                matched_company, prospeo_company_name, prospeo_company_domain = company_match(row, item)
                row["prospeo_company_name"] = prospeo_company_name
                row["prospeo_company_domain"] = prospeo_company_domain
                row["prospeo_company_match"] = "yes" if matched_company else "no"
                if matched_company:
                    company_matched += 1
                email_value, email_status, email_payload = prospeo_email_fields(item)
                if email_value and matched_company:
                    row["person_email"] = email_value
                if matched_company:
                    row["person_email_status"] = f"prospeo_{email_status.lower()}" if email_status else "prospeo_matched"
                else:
                    row["person_email_status"] = "prospeo_company_mismatch"
                row["prospeo_email_status"] = email_status
                row["prospeo_free_enrichment"] = str(bool(item.get("free_enrichment") or data.get("response", {}).get("free_enrichment") or data.get("free_enrichment")))
                person = item.get("person") if isinstance(item.get("person"), dict) else {}
                row["prospeo_person_id"] = str(person.get("person_id") or "")
                if matched_company and not email_value and email_payload:
                    row["person_email_status"] = row.get("person_email_status") or "prospeo_unrevealed"

    completed_at = utc_stamp()
    write_rows(output_csv, rows, fieldnames)
    summary = {
        "input_csv": str(input_csv),
        "output_csv": str(output_csv),
        "raw_jsonl": str(raw_jsonl),
        "started_at": started_at,
        "completed_at": completed_at,
        "row_count": len(rows),
        "attempted_rows": len(pending_records),
        "matched": matched,
        "company_matched": company_matched,
        "not_matched": not_matched,
        "invalid_datapoints": invalid,
        "emails_found": sum(1 for row in rows if (row.get("person_email") or "").strip()),
        "only_verified_email": only_verified_email,
        "batch_size": batch_size,
        "total_batches": total_batches,
        "estimated_credits_spent": total_cost,
    }
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--raw-jsonl", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--only-verified-email", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    asyncio.run(
        enrich(
            input_csv=Path(args.input_csv),
            output_csv=Path(args.output_csv),
            raw_jsonl=Path(args.raw_jsonl),
            summary_json=Path(args.summary_json),
            only_verified_email=bool(args.only_verified_email),
            batch_size=max(1, min(int(args.batch_size), 50)),
            max_rows=max(0, int(args.max_rows)),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
