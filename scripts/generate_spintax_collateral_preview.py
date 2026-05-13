#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


def _set_csv_field_limit() -> None:
    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            return
        except OverflowError:
            field_limit //= 10


@dataclass(frozen=True)
class TemplateGroup:
    key: tuple[str, str, str, str, str, str, str, str]
    label: str
    rows: list[dict[str, str]]


S1_TEMPLATE_LABELS = {
    "{quiet client risk|client visibility gap|delivery risk signal|msp risk drift}": "Leadership - MSP/MSSP",
    "{client trust risk|renewal proof gap|delivery visibility gap|account risk signal}": "Commercial - MSP/MSSP",
    "{evidence trail gap|quiet alert gap|review proof gap|control trail gap}": "Security - MSP/MSSP",
    "{change visibility gap|client drift risk|shared tool drift|ops trail gap}": "IT Ops - MSP/MSSP",
    "{follow up drag|evidence chase gap|review coordination risk|admin trail gap}": "Administrative - MSP/MSSP",
}


def _load_rows(path: Path) -> list[dict[str, str]]:
    _set_csv_field_limit()
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _template_key(row: dict[str, str]) -> tuple[str, str, str, str, str, str, str, str]:
    return tuple(row.get(field, "") for field in ("s1", "e1", "s2", "e2", "s3", "e3", "s4", "e4"))


def _cluster_templates(rows: list[dict[str, str]]) -> list[TemplateGroup]:
    grouped: dict[tuple[str, str, str, str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[_template_key(row)].append(row)

    ordered: list[TemplateGroup] = []
    for key, cluster_rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0][0])):
        s1 = key[0]
        label = S1_TEMPLATE_LABELS.get(s1) or f"Template {len(ordered) + 1}"
        ordered.append(TemplateGroup(key=key, label=label, rows=cluster_rows))
    return ordered


def _top_values(rows: list[dict[str, str]], field: str, limit: int) -> list[str]:
    counter = Counter()
    for row in rows:
        value = " ".join((row.get(field, "") or "").split())
        if value:
            counter[value] += 1
    return [value for value, _ in counter.most_common(limit)]


def _sample_value(rows: list[dict[str, str]], field: str) -> str:
    for row in rows:
        value = " ".join((row.get(field, "") or "").split())
        if value:
            return value
    return ""


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_frontmatter(*, slug: str, title: str, created_at: str, campaign_name: str) -> str:
    lines = [
        "---",
        "doc_type: spintax_report",
        f"slug: {slug}",
        f"title: {title}",
        "client_name: Alertica",
        "client_logo_svg: alertica.svg",
        "brand_template: spintax-report-v1",
        "owner: mortonstreet",
        "status: draft",
        f"created_at: {created_at}",
        "sequence_steps: 4",
        "export_targets:",
        "  - html",
        "campaign:",
        f"  campaign_id: {slug.replace('-', '_')}",
        f"  campaign_name: {_yaml_quote(campaign_name)}",
        '  sender_name: "Arik Liberman"',
        '  sender_title: "Founder, Alertica"',
        '  offer_name: "agentless change visibility for MSP and MSSP teams"',
        '  proof_anchor: "client-path drift detection across shared tools and environments"',
        '  cta_type: "short outline"',
        "---",
        "",
    ]
    return "\n".join(lines)


def _render_overview(*, people_count: int, company_count: int, bucket_counts: dict[str, int]) -> str:
    ordered_buckets = ", ".join(f"{bucket}={count}" for bucket, count in bucket_counts.items())
    lines = [
        "## Overview",
        "",
        (
            f"MSP/MSSP outbound spintax preview generated from the finalized Alertica campaign-prep run. "
            f"This pack covers {people_count} qualified people rows across {company_count} researched companies. "
            f"Each template below is grouped from the final spintax CSV by shared sequence copy, with sample role coverage "
            f"and example personalization lines distilled from company context. Current mix: {ordered_buckets}."
        ),
        "",
        "## Signature",
        "",
        "```txt",
        "{Best, | Thanks,}",
        "",
        "Arik Liberman",
        "Founder, Alertica",
        "```",
        "",
    ]
    return "\n".join(lines)


def _render_template(group: TemplateGroup) -> str:
    top_roles = _top_values(group.rows, "role", 8)
    top_companies = _top_values(group.rows, "company_name", 4)
    sample_p1 = _sample_value(group.rows, "p1")
    sample_p2 = _sample_value(group.rows, "p2")
    sample_p3 = _sample_value(group.rows, "p3")
    sample_p4 = _sample_value(group.rows, "p4")
    s1, e1, s2, e2, s3, e3, s4, e4 = group.key
    lines = [
        f"## Template {group.label}",
        "",
        "### Audience",
        "",
        "- Segment: msp_mssp",
        f"- Roles: {', '.join(top_roles)}",
        f"- Rows represented: {len(group.rows)}",
        f"- Sample companies: {', '.join(top_companies)}",
        f"- Sample p1: {sample_p1}",
        f"- Sample p2: {sample_p2}",
        f"- Sample p3: {sample_p3}",
        f"- Sample p4: {sample_p4}",
        "",
    ]

    for idx, (subject, body) in enumerate(((s1, e1), (s2, e2), (s3, e3), (s4, e4)), start=1):
        lines.extend(
            [
                f"### Step {idx}",
                "",
                f"#### Subject (s{idx})",
                "",
                "```txt",
                subject,
                "```",
                "",
                f"#### Body (e{idx})",
                "",
                "```txt",
                body,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--people-csv", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--title", default="Alertica MSP/MSSP Spintax Report")
    parser.add_argument("--slug", default="alertica-msp-mssp-copygen-preview")
    parser.add_argument("--campaign-name", default="Alertica MSP/MSSP Copygen Preview")
    parser.add_argument("--created-at", default="2026-05-12")
    args = parser.parse_args()

    people_csv = Path(args.people_csv)
    summary_json = Path(args.summary_json)
    output_md = Path(args.output_md)

    rows = _load_rows(people_csv)
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    groups = _cluster_templates(rows)

    markdown_parts = [
        _render_frontmatter(
            slug=args.slug,
            title=args.title,
            created_at=args.created_at,
            campaign_name=args.campaign_name,
        ),
        _render_overview(
            people_count=int(summary.get("row_count") or summary.get("people_rows") or len(rows)),
            company_count=int(summary.get("company_count") or summary.get("company_rows") or 0),
            bucket_counts=summary.get("bucket_counts") or {},
        ),
    ]
    markdown_parts.extend(_render_template(group) for group in groups)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text("\n".join(markdown_parts).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
