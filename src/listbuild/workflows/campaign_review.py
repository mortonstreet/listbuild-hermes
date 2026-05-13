from __future__ import annotations

import csv
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import sys
from typing import Any

from listbuild.runs import build_run_paths, scaffold_run_directories
from listbuild.workflows.campaign_prep import (
    CAMPAIGN_PROFILE_AUTO,
    CAMPAIGN_PROFILE_FQHC,
    CAMPAIGN_PROFILE_GENERIC,
    CAMPAIGN_PROFILE_MSP_MSSP,
    _campaign_bucket,
    _clean_text,
    _resolve_campaign_profile,
    _role_title,
    _row_matches_campaign_profile,
)


@dataclass(frozen=True, slots=True)
class CampaignReviewPolicy:
    people_csv: str
    qualifier_only: bool = True
    max_people: int = 0
    campaign_profile: str = CAMPAIGN_PROFILE_AUTO


@dataclass(frozen=True, slots=True)
class CampaignReviewRun:
    run_slug: str
    campaign_profile: str
    reviewed_people_count: int
    reviewed_company_count: int
    direction_md: str
    signal_review_csv: str
    segment_review_csv: str
    sequence_review_md: str
    copy_style_md: str
    copy_controls_json: str
    review_html: str
    summary_json: str
    segment_counts: dict[str, int]
    signal_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "campaign_profile": self.campaign_profile,
            "reviewed_people_count": self.reviewed_people_count,
            "reviewed_company_count": self.reviewed_company_count,
            "direction_md": self.direction_md,
            "signal_review_csv": self.signal_review_csv,
            "segment_review_csv": self.segment_review_csv,
            "sequence_review_md": self.sequence_review_md,
            "copy_style_md": self.copy_style_md,
            "copy_controls_json": self.copy_controls_json,
            "review_html": self.review_html,
            "summary_json": self.summary_json,
            "segment_counts": self.segment_counts,
            "signal_counts": self.signal_counts,
        }


@dataclass(frozen=True, slots=True)
class SignalReviewRow:
    company_name: str
    company_domain: str
    source_type: str
    raw_signal: str
    cleaned_signal: str
    signal_disposition: str
    signal_reason: str
    signal_copy_use: str
    suggested_sequence_angle: str

    @classmethod
    def headers(cls) -> list[str]:
        return list(cls.__dataclass_fields__.keys())

    def to_dict(self) -> dict[str, str]:
        return {key: str(getattr(self, key) or "") for key in self.headers()}


@dataclass(frozen=True, slots=True)
class SegmentReviewRow:
    company_name: str
    role_title: str
    role_description: str
    current_segment: str
    suggested_segment: str
    segment_disposition: str
    segment_reason: str

    @classmethod
    def headers(cls) -> list[str]:
        return list(cls.__dataclass_fields__.keys())

    def to_dict(self) -> dict[str, str]:
        return {key: str(getattr(self, key) or "") for key in self.headers()}


DROP_SIGNAL_PATTERNS = (
    "#",
    "starwarsday",
    "maythe4th",
    "happy ",
    "celebrat",
    "proud to",
    "check out",
    "join us",
    "follow us",
    "watch now",
    "register now",
    "our team had a great time",
)

KEEP_SIGNAL_PATTERNS = (
    "hiring",
    "expanding",
    "expansion",
    "opened",
    "launch",
    "launched",
    "migration",
    "acquisition",
    "acquired",
    "merger",
    "partner",
    "partnership",
    "certified",
    "compliance",
    "audit",
    "review",
    "security",
    "cyber",
    "soc",
    "siem",
    "mdr",
    "xdr",
    "backup",
    "disaster recovery",
    "cloud",
    "data center",
    "network",
    "infrastructure",
    "managed services",
    "managed security",
    "onboarding",
    "transition",
    "incident",
    "threat",
    "client",
    "tenant",
)

REVIEW_SIGNAL_PATTERNS = (
    "award",
    "awards",
    "recognition",
    "named",
    "top ",
    "conference",
    "webinar",
    "event",
    "summit",
    "podcast",
    "episode",
    "training",
)

DROP_ROLE_PATTERNS = (
    "recruit",
    "talent",
    "payroll",
    "fp&a",
    "controller",
    "tax",
    "logistic",
    "import",
    "export",
)

SEGMENT_CONFIG = {
    "leadership": {
        "display": "Leadership",
        "recommended": "Problem Sniffing",
        "value_rotation": "Step 1 save time and risk. Step 2 make money through client trust. Step 3 save money via less cleanup. Step 4 routing or outline offer.",
        "why": "Leadership should hear quiet client risk, service visibility gaps, and time pulled into late explanations.",
        "option_1_subject": "quiet client risk",
        "option_1_first_line": "Saw {{approved_signal}} at {{company_name}}. Usually means more client-side risk can pile up quietly across environments.",
        "option_2_subject": "client visibility gap",
        "option_2_first_line": "How are you currently keeping clean visibility across shared tooling and client environments without adding more operational drag?",
        "option_3_subject": "question for {{first_name}}",
        "option_3_first_line": "Looks like {{company_name}} is scaling managed services across multiple environments. Is quiet change drift already buttoned up, or still manual to untangle?",
    },
    "it_ops": {
        "display": "IT Ops",
        "recommended": "Problem Sniffing",
        "value_rotation": "Step 1 save time on escalations. Step 2 save money on cleanup. Step 3 make money by protecting service quality. Step 4 path-review or outline.",
        "why": "IT ops should hear change visibility, shared-tool drift, and service-handoff pressure in concrete terms.",
        "option_1_subject": "change visibility gap",
        "option_1_first_line": "Saw {{approved_signal}} at {{company_name}}. Usually means more shared tools, client-side changes, and handoffs to keep visible.",
        "option_2_subject": "shared tool drift",
        "option_2_first_line": "How are you tracking client-path drift today when an escalation turns into tracing what changed, where, and when?",
        "option_3_subject": "tooling trail gap",
        "option_3_first_line": "Looks like {{company_name}} has more delivery-critical paths in motion. Is the current visibility clean enough once the queue heats up?",
    },
    "security_compliance": {
        "display": "Security / Compliance",
        "recommended": "Problem Sniffing",
        "value_rotation": "Step 1 save time on evidence pulls. Step 2 save money through less review drag. Step 3 protect revenue and trust by reducing surprise client risk. Step 4 outline or redirect.",
        "why": "Security and legal-risk owners should hear evidence quality, alert noise, and defensible trail language instead of generic service copy.",
        "option_1_subject": "evidence trail gap",
        "option_1_first_line": "Saw {{approved_signal}} at {{company_name}}. Usually raises the pressure to separate real risk from noise while keeping a defensible trail.",
        "option_2_subject": "review proof gap",
        "option_2_first_line": "How are you proving what changed and when across client environments once a review or evidence request lands?",
        "option_3_subject": "control trail gap",
        "option_3_first_line": "Looks like {{company_name}} has more evidence-sensitive paths in motion. Is the current trail strong enough when teams need answers fast?",
    },
    "commercial_client": {
        "display": "Commercial / Client",
        "recommended": "Billboard",
        "value_rotation": "Step 1 protect client trust. Step 2 make money through renewals. Step 3 save time on client follow-up. Step 4 quick outline or redirect.",
        "why": "Commercial roles do not need deep technical detail first. They need delivery-proof language tied to renewals and client confidence.",
        "option_1_subject": "client trust gap",
        "option_1_first_line": "When delivery spans more environments, quiet change issues can turn into client questions before account teams have a clean answer ready.",
        "option_2_subject": "renewal proof gap",
        "option_2_first_line": "Saw {{approved_signal}} at {{company_name}}. Usually means account teams need cleaner delivery proof before a client asks for answers.",
        "option_3_subject": "question for {{first_name}}",
        "option_3_first_line": "Is client-side follow-up around what changed and how quickly the team caught it already easy at {{company_name}}, or still heavier than it should be?",
    },
    "admin_legal": {
        "display": "Admin / Coordination",
        "recommended": "AI Generic",
        "value_rotation": "Step 1 save time on chase work. Step 2 save money on rework. Step 3 protect client trust through cleaner coordination. Step 4 redirect or short outline.",
        "why": "Admin and coordination roles should hear evidence chase, documentation drag, and handoff burden in plain language.",
        "option_1_subject": "follow up drag",
        "option_1_first_line": "When the change trail is thin, coordination teams usually end up carrying the follow-up and documentation chase.",
        "option_2_subject": "review coordination risk",
        "option_2_first_line": "Saw {{approved_signal}} at {{company_name}}. Usually means more review and coordination work once people need to confirm what changed and when.",
        "option_3_subject": "documentation drag",
        "option_3_first_line": "Is the current trail at {{company_name}} already clean enough for review and handoff-heavy workflows, or does too much still get chased manually?",
    },
}

SEGMENT_STYLE_GUIDES = {
    "leadership": {
        "jargon_level": "low",
        "word_target": "45-75 words",
        "first_line_target": "8-14 words",
        "tone": "plainspoken, operator-level, low-ego",
        "notes": "Business risk and delivery drag first. Do not lead with acronyms unless the signal requires it.",
    },
    "it_ops": {
        "jargon_level": "medium",
        "word_target": "50-85 words",
        "first_line_target": "8-14 words",
        "tone": "practical, technical, still conversational",
        "notes": "Use technical framing only when it maps to a real operating pain like drift, handoffs, or escalations.",
    },
    "security_compliance": {
        "jargon_level": "medium-high",
        "word_target": "50-90 words",
        "first_line_target": "8-14 words",
        "tone": "credible, calm, evidence-oriented",
        "notes": "Use security and compliance language carefully. Avoid acronym stacking or fear-heavy phrasing.",
    },
    "commercial_client": {
        "jargon_level": "low",
        "word_target": "45-75 words",
        "first_line_target": "8-12 words",
        "tone": "client-facing, clear, commercially aware",
        "notes": "Avoid heavy technical detail. Lead with renewals, delivery proof, or client confidence.",
    },
    "admin_legal": {
        "jargon_level": "low-medium",
        "word_target": "45-75 words",
        "first_line_target": "8-12 words",
        "tone": "clear, organized, coordination-focused",
        "notes": "Talk like a person reducing follow-up drag, not a compliance whitepaper.",
    },
}


def _set_csv_field_limit() -> None:
    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            return
        except OverflowError:
            field_limit //= 10


def _split_signal_fragments(value: str, *, separators: tuple[str, ...]) -> list[str]:
    if not value.strip():
        return []
    parts = [value]
    for separator in separators:
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(part.split(separator))
        parts = next_parts
    return [_clean_text(part) for part in parts if _clean_text(part)]


def _clean_signal_fragment(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    return text


def _classify_signal(cleaned_signal: str, *, raw_signal: str) -> tuple[str, str, str, str]:
    lowered_raw = raw_signal.casefold()
    lowered = cleaned_signal.casefold()
    if not cleaned_signal or len(cleaned_signal) < 18:
        return "drop", "signal collapses after cleaning or is too short", "", ""
    if any(pattern in lowered_raw for pattern in DROP_SIGNAL_PATTERNS):
        return "drop", "social, hashtag, or engagement noise not useful for outbound", "", ""
    if any(pattern in lowered for pattern in KEEP_SIGNAL_PATTERNS):
        if any(term in lowered for term in ("hiring", "expanding", "expansion", "opened", "launch", "launched", "acquisition", "acquired", "merger", "partner", "partnership", "migration", "onboarding", "transition")):
            return "keep", "concrete operating or go-to-market motion", "p1", "problem sniffing"
        if any(term in lowered for term in ("security", "cyber", "soc", "siem", "mdr", "xdr", "compliance", "audit", "review", "incident", "threat")):
            return "keep", "security or compliance context with copy value", "p1", "problem sniffing"
        return "keep", "service or infrastructure context can support copy", "p1", "problem sniffing"
    if any(pattern in lowered for pattern in REVIEW_SIGNAL_PATTERNS):
        return "review", "recognition or event signal may be usable only if tied to operations or security", "p1", "billboard"
    return "review", "ambiguous signal needs human review before copy use", "", ""


def _collapse_current_segment(row: dict[str, str], *, campaign_profile: str) -> str:
    bucket = _campaign_bucket(row, campaign_profile=campaign_profile)
    if bucket == "security":
        return "security_compliance"
    if bucket == "commercial":
        return "commercial_client"
    if bucket == "admin":
        return "admin_legal"
    if bucket == "it_ops":
        return "it_ops"
    return "leadership"


def _suggest_segment(row: dict[str, str], *, campaign_profile: str) -> tuple[str, str, str]:
    title = _role_title(row).casefold()
    if any(token in title for token in DROP_ROLE_PATTERNS):
        return "drop", "drop", "role is outside the MSP/MSSP buyer map"
    if any(token in title for token in ("security", "cyber", "soc", "ciso", "risk", "compliance", "privacy", "legal", "audit", "governance")):
        return "security_compliance", "keep", "role is directly tied to security, legal, or compliance risk"
    if any(token in title for token in ("account manager", "account management", "business development", "sales", "channel", "partner", "customer success", "client success", "client advisor", "client relations", "corporate relations", "commercial", "revenue", "growth", "vertical solutions")):
        return "commercial_client", "keep", "role is client-facing or growth-oriented"
    if any(token in title for token in ("system administrator", "systems administrator", "linux system administrator", "network administrator", "database administrator", "it ", " it", "system", "systems", "infrastructure", "network", "cloud", "helpdesk", "help desk", "support", "service delivery", "operations", "technical", "technology", "solutions architect", "engineer", "architect")):
        return "it_ops", "keep", "role is operational or technical"
    if any(token in title for token in ("assistant", "office manager", "administrative", "coordinator", "executive assistant", "secretary")):
        return "admin_legal", "keep", "role is closer to coordination, follow-up, and documentation"
    if any(token in title for token in ("ceo", "president", "owner", "founder", "co-founder", "coo", "cfo", "cmo", "cio", "cto", "chief", "managing partner", "managing director", "vice president", "vp ")):
        return "leadership", "keep", "role carries leadership or ownership responsibility"
    current_segment = _collapse_current_segment(row, campaign_profile=campaign_profile)
    return current_segment, "review", "title is adjacent or ambiguous and should be reviewed"


class CampaignReviewWorkflow:
    def run(
        self,
        *,
        seller: str,
        segment: str,
        date_stamp: str | None = None,
        root: str = "runs",
        policy: CampaignReviewPolicy,
    ) -> CampaignReviewRun:
        paths = build_run_paths(
            seller=seller,
            segment=segment,
            date_stamp=date_stamp,
            root=root,
        )
        scaffold_run_directories(paths)
        direction_path = paths.output_dir / f"{paths.naming.run_slug}-direction-review.md"
        signal_review_path = paths.output_dir / f"{paths.naming.run_slug}-signal-review.csv"
        segment_review_path = paths.output_dir / f"{paths.naming.run_slug}-segment-review.csv"
        sequence_review_path = paths.output_dir / f"{paths.naming.run_slug}-sequence-review.md"
        copy_style_path = paths.output_dir / f"{paths.naming.run_slug}-copy-style-review.md"
        copy_controls_path = paths.output_dir / f"{paths.naming.run_slug}-copy-controls.json"
        review_html_path = paths.output_dir / f"{paths.naming.run_slug}-review.html"
        summary_path = paths.output_dir / f"{paths.naming.run_slug}-summary.json"

        campaign_profile = _resolve_campaign_profile(
            policy_value=policy.campaign_profile,
            seller=seller,
            segment=segment,
        )
        rows = self._read_csv(policy.people_csv)
        filtered_rows = self._filter_rows(
            rows=rows,
            qualifier_only=policy.qualifier_only,
            max_people=policy.max_people,
            campaign_profile=campaign_profile,
        )
        signal_rows = self._build_signal_review(filtered_rows)
        segment_rows = self._build_segment_review(filtered_rows, campaign_profile=campaign_profile)
        company_count = len(
            {
                (_clean_text(row.get("company_domain")) or _clean_text(row.get("company_name"))).casefold()
                for row in filtered_rows
                if _clean_text(row.get("company_name")) or _clean_text(row.get("company_domain"))
            }
        )

        signal_counts = self._count_values(signal_rows, key=lambda item: item.signal_disposition)
        segment_counts = self._count_values(segment_rows, key=lambda item: item.suggested_segment)

        self._write_csv(signal_review_path, SignalReviewRow.headers(), [row.to_dict() for row in signal_rows])
        self._write_csv(segment_review_path, SegmentReviewRow.headers(), [row.to_dict() for row in segment_rows])
        direction_markdown = self._render_direction_review(
            seller=seller,
            campaign_profile=campaign_profile,
            filtered_rows=filtered_rows,
            company_count=company_count,
            signal_rows=signal_rows,
            segment_rows=segment_rows,
            signal_counts=signal_counts,
            segment_counts=segment_counts,
        )
        direction_path.write_text(direction_markdown, encoding="utf-8")
        sequence_markdown = self._render_sequence_review(
            campaign_profile=campaign_profile,
            segment_rows=segment_rows,
            segment_counts=segment_counts,
        )
        sequence_review_path.write_text(sequence_markdown, encoding="utf-8")
        copy_style_markdown = self._render_copy_style_review(campaign_profile=campaign_profile)
        copy_style_path.write_text(copy_style_markdown, encoding="utf-8")
        copy_controls = self._build_copy_controls(campaign_profile=campaign_profile)
        copy_controls_path.write_text(json.dumps(copy_controls, indent=2) + "\n", encoding="utf-8")
        review_html = self._render_review_html(
            seller=seller,
            campaign_profile=campaign_profile,
            reviewed_people_count=len(filtered_rows),
            company_count=company_count,
            signal_rows=signal_rows,
            segment_rows=segment_rows,
            signal_counts=signal_counts,
            segment_counts=segment_counts,
        )
        review_html_path.write_text(review_html, encoding="utf-8")

        summary_payload = {
            "run_slug": paths.naming.run_slug,
            "campaign_profile": campaign_profile,
            "reviewed_people_count": len(filtered_rows),
            "reviewed_company_count": company_count,
            "signal_counts": signal_counts,
            "segment_counts": segment_counts,
            "direction_md": str(direction_path),
            "signal_review_csv": str(signal_review_path),
            "segment_review_csv": str(segment_review_path),
            "sequence_review_md": str(sequence_review_path),
            "copy_style_md": str(copy_style_path),
            "copy_controls_json": str(copy_controls_path),
            "review_html": str(review_html_path),
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2) + "\n", encoding="utf-8")
        return CampaignReviewRun(
            run_slug=paths.naming.run_slug,
            campaign_profile=campaign_profile,
            reviewed_people_count=len(filtered_rows),
            reviewed_company_count=company_count,
            direction_md=str(direction_path),
            signal_review_csv=str(signal_review_path),
            segment_review_csv=str(segment_review_path),
            sequence_review_md=str(sequence_review_path),
            copy_style_md=str(copy_style_path),
            copy_controls_json=str(copy_controls_path),
            review_html=str(review_html_path),
            summary_json=str(summary_path),
            segment_counts=segment_counts,
            signal_counts=signal_counts,
        )

    @staticmethod
    def _read_csv(path: str) -> list[dict[str, str]]:
        _set_csv_field_limit()
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def _filter_rows(
        *,
        rows: list[dict[str, str]],
        qualifier_only: bool,
        max_people: int,
        campaign_profile: str,
    ) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for row in rows:
            if qualifier_only and _clean_text(row.get("person_soft_qualifies")).casefold() not in {"yes", "true", "1"}:
                continue
            if not _row_matches_campaign_profile(row, campaign_profile=campaign_profile):
                continue
            filtered.append(row)
        if max_people > 0:
            return filtered[:max_people]
        return filtered

    def _build_signal_review(self, rows: list[dict[str, str]]) -> list[SignalReviewRow]:
        seen: set[tuple[str, str, str]] = set()
        review_rows: list[SignalReviewRow] = []
        for row in rows:
            company_name = _clean_text(row.get("company_name"))
            company_domain = _clean_text(row.get("company_domain"))
            for raw_signal in _split_signal_fragments(_clean_text(row.get("company_signals")), separators=("|",)):
                key = (company_name.casefold(), "company_signals", raw_signal.casefold())
                if key in seen:
                    continue
                seen.add(key)
                cleaned = _clean_signal_fragment(raw_signal)
                disposition, reason, copy_use, sequence_angle = _classify_signal(cleaned, raw_signal=raw_signal)
                review_rows.append(
                    SignalReviewRow(
                        company_name=company_name,
                        company_domain=company_domain,
                        source_type="company_signals",
                        raw_signal=raw_signal,
                        cleaned_signal=cleaned,
                        signal_disposition=disposition,
                        signal_reason=reason,
                        signal_copy_use=copy_use,
                        suggested_sequence_angle=sequence_angle,
                    )
                )
            for raw_signal in _split_signal_fragments(_clean_text(row.get("company_recent_post_summary")), separators=("||",)):
                key = (company_name.casefold(), "recent_posts", raw_signal.casefold())
                if key in seen:
                    continue
                seen.add(key)
                cleaned = _clean_signal_fragment(raw_signal)
                disposition, reason, copy_use, sequence_angle = _classify_signal(cleaned, raw_signal=raw_signal)
                review_rows.append(
                    SignalReviewRow(
                        company_name=company_name,
                        company_domain=company_domain,
                        source_type="recent_posts",
                        raw_signal=raw_signal,
                        cleaned_signal=cleaned,
                        signal_disposition=disposition,
                        signal_reason=reason,
                        signal_copy_use=copy_use,
                        suggested_sequence_angle=sequence_angle,
                    )
                )
        return sorted(
            review_rows,
            key=lambda item: (
                {"keep": 0, "review": 1, "drop": 2}.get(item.signal_disposition, 3),
                item.company_name.casefold(),
                item.source_type,
                item.cleaned_signal.casefold(),
            ),
        )

    def _build_segment_review(
        self,
        rows: list[dict[str, str]],
        *,
        campaign_profile: str,
    ) -> list[SegmentReviewRow]:
        review_rows: list[SegmentReviewRow] = []
        for row in rows:
            current_segment = _collapse_current_segment(row, campaign_profile=campaign_profile)
            suggested_segment, default_disposition, reason = _suggest_segment(row, campaign_profile=campaign_profile)
            disposition = default_disposition
            if suggested_segment not in {"drop"} and current_segment != suggested_segment and default_disposition != "review":
                disposition = "reclassify"
                reason = f"current segment looks like {current_segment}, but title is a better fit for {suggested_segment}"
            review_rows.append(
                SegmentReviewRow(
                    company_name=_clean_text(row.get("company_name")),
                    role_title=_role_title(row),
                    role_description=_clean_text(row.get("harvest_about") or row.get("role_guess") or row.get("harvest_headline")),
                    current_segment=current_segment,
                    suggested_segment=suggested_segment,
                    segment_disposition=disposition,
                    segment_reason=reason,
                )
            )
        return sorted(
            review_rows,
            key=lambda item: (
                {"reclassify": 0, "review": 1, "drop": 2, "keep": 3}.get(item.segment_disposition, 4),
                item.suggested_segment,
                item.company_name.casefold(),
                item.role_title.casefold(),
            ),
        )

    @staticmethod
    def _count_values[T](items: list[T], *, key: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            value = str(key(item) or "")
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))

    @staticmethod
    def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def _render_direction_review(
        self,
        *,
        seller: str,
        campaign_profile: str,
        filtered_rows: list[dict[str, str]],
        company_count: int,
        signal_rows: list[SignalReviewRow],
        segment_rows: list[SegmentReviewRow],
        signal_counts: dict[str, int],
        segment_counts: dict[str, int],
    ) -> str:
        top_kept_signals = [row.cleaned_signal for row in signal_rows if row.signal_disposition == "keep"][:8]
        top_dropped_signals = [row.raw_signal for row in signal_rows if row.signal_disposition == "drop"][:8]
        top_roles_by_segment: dict[str, list[str]] = {}
        for segment in SEGMENT_CONFIG:
            roles: list[str] = []
            for row in segment_rows:
                if row.suggested_segment == segment and row.role_title and row.role_title not in roles:
                    roles.append(row.role_title)
                if len(roles) >= 8:
                    break
            top_roles_by_segment[segment] = roles

        lines: list[str] = []
        lines.append("# Step 1: Campaign Direction")
        lines.append("")
        lines.append(f"- Seller: `{seller}`")
        lines.append(f"- Campaign profile: `{campaign_profile}`")
        lines.append(f"- Qualified people reviewed: `{len(filtered_rows)}`")
        lines.append(f"- Companies represented: `{company_count}`")
        lines.append("")
        lines.append("**Target Audience:** MSP and MSSP operators, security owners, commercial/client owners, and coordination-heavy roles close to delivery proof.")
        lines.append("**Core Pain Point:** quiet change drift, evidence gaps, alert noise, client-environment complexity, and client-facing follow-up when teams cannot explain what changed.")
        lines.append("**Value Proposition:** agentless visibility across client paths, shared tooling, and evidence-sensitive file flows before quiet drift becomes delivery, review, or renewal pain.")
        lines.append("**Proof Point Direction:** company-site research, LinkedIn company activity, and operational signals tied to delivery, security, compliance, onboarding, or expansion.")
        lines.append("")
        lines.append("**Recommended Segment Model:**")
        for segment_key, count in segment_counts.items():
            if segment_key == "drop":
                continue
            display = SEGMENT_CONFIG.get(segment_key, {}).get("display", segment_key)
            role_examples = ", ".join(top_roles_by_segment.get(segment_key, [])[:5])
            lines.append(f"- `{segment_key}` ({display}): `{count}` rows")
            lines.append(f"  Examples: {role_examples or 'n/a'}")
        lines.append("")
        lines.append("**Signal Hygiene Policy:**")
        lines.append("- Keep only operating, delivery, security, compliance, onboarding, migration, hiring, partner, expansion, and client-environment signals.")
        lines.append("- Treat company description and offer as background context for body copy, not as first-line signal sources.")
        lines.append("- Drop hashtags, holiday posts, generic social content, culture filler, and weak recognition blurbs unless they map to a real operating motion.")
        lines.append("")
        lines.append("**AI Variables Available:**")
        lines.append("- `company_offer`: service mix and delivery scope")
        lines.append("- `company_painpoint`: distilled operational risk hypothesis")
        lines.append("- `company_signals`: approved operating motions only after hygiene review")
        lines.append("- `company_recent_post_summary`: post-derived context, but only after junk filtering")
        lines.append("- `role_title` and `role_description`: segment framing and CTA tone")
        lines.append("")
        lines.append("**Signal Review Snapshot:**")
        for disposition, count in signal_counts.items():
            lines.append(f"- `{disposition}`: `{count}`")
        lines.append("")
        lines.append("Kept signal examples:")
        for example in top_kept_signals:
            lines.append(f"- {example}")
        lines.append("")
        lines.append("Dropped signal examples:")
        for example in top_dropped_signals:
            lines.append(f"- {example}")
        lines.append("")
        lines.append("Does this direction work before we lock segment signoff and sequence options?")
        lines.append("")
        return "\n".join(lines)

    def _render_sequence_review(
        self,
        *,
        campaign_profile: str,
        segment_rows: list[SegmentReviewRow],
        segment_counts: dict[str, int],
    ) -> str:
        lines: list[str] = []
        lines.append("# Step 2: Subject Line + Sequence Strategy")
        lines.append("")
        lines.append(f"- Campaign profile: `{campaign_profile}`")
        lines.append("- These are review options only. No final copy should be materialized until segment and strategy signoff is complete.")
        lines.append("")
        for segment_key in ("leadership", "it_ops", "security_compliance", "commercial_client", "admin_legal"):
            count = segment_counts.get(segment_key, 0)
            if count == 0:
                continue
            config = SEGMENT_CONFIG[segment_key]
            example_roles: list[str] = []
            for row in segment_rows:
                if row.suggested_segment == segment_key and row.role_title and row.role_title not in example_roles:
                    example_roles.append(row.role_title)
                if len(example_roles) >= 6:
                    break
            lines.append(f"## {config['display']}")
            lines.append("")
            lines.append(f"- Rows: `{count}`")
            lines.append(f"- Recommended strategy: **{config['recommended']}**")
            lines.append(f"- Why: {config['why']}")
            lines.append(f"- Example roles: {', '.join(example_roles) or 'n/a'}")
            lines.append(f"- Value-prop rotation: {config['value_rotation']}")
            lines.append("")
            lines.append("### Option 1")
            lines.append("")
            lines.append(f"- Strategy: {config['recommended']}")
            lines.append(f"- Subject: `{config['option_1_subject']}`")
            lines.append(f"- First line pattern: {config['option_1_first_line']}")
            lines.append("")
            lines.append("### Option 2")
            lines.append("")
            lines.append("- Strategy: Billboard")
            lines.append(f"- Subject: `{config['option_2_subject']}`")
            lines.append(f"- First line pattern: {config['option_2_first_line']}")
            lines.append("")
            lines.append("### Option 3")
            lines.append("")
            lines.append("- Strategy: AI Generic")
            lines.append(f"- Subject: `{config['option_3_subject']}`")
            lines.append(f"- First line pattern: {config['option_3_first_line']}")
            lines.append("")
            lines.append("Approve one default strategy for this segment before final spintax generation.")
            lines.append("")
        return "\n".join(lines)

    def _render_copy_style_review(self, *, campaign_profile: str) -> str:
        lines: list[str] = []
        lines.append("# Step 3: Copy Style Review")
        lines.append("")
        lines.append(f"- Campaign profile: `{campaign_profile}`")
        lines.append("- This stage is for human-in-the-loop editing before final spintax generation.")
        lines.append("- The goal is to make the copy sound like a real person sending a clear note, not a model explaining itself.")
        lines.append("")
        lines.append("## Global Rubric")
        lines.append("")
        lines.append("- First line target: `8-14 words`, max `16` unless the signal absolutely needs more room.")
        lines.append("- Email target: `45-85 words` for most segments. Security / IT ops can stretch slightly if the specificity earns it.")
        lines.append("- Keep sentence count low: usually `3-5` short sentences.")
        lines.append("- Prefer one thought per sentence. Avoid stacked clauses and overloaded list phrasing.")
        lines.append("- Sound like a clear text or conversational note. Avoid whitepaper language and robotic transitions.")
        lines.append("- Never use raw social copy, hashtags, or post slogans as first-line personalization.")
        lines.append("- Approved signals can lead the email only if they describe a real operating motion or delivery reality.")
        lines.append("")
        lines.append("## Human Review Questions")
        lines.append("")
        lines.append("- Did this actually get shorter, or did it only sound more human while staying too long?")
        lines.append("- Is the first line under the target word count and easy to read out loud?")
        lines.append("- Does the message sound conversational and organic, or does it read like assembled campaign logic?")
        lines.append("- Is the technical jargon dialed appropriately for the role segment?")
        lines.append("- Can one sentence be split into two cleaner ones without losing meaning?")
        lines.append("- Is the CTA low-friction and reply-friendly?")
        lines.append("")
        lines.append("## Segment Style Defaults")
        lines.append("")
        for segment_key in ("leadership", "it_ops", "security_compliance", "commercial_client", "admin_legal"):
            guide = SEGMENT_STYLE_GUIDES[segment_key]
            lines.append(f"### {SEGMENT_CONFIG[segment_key]['display']}")
            lines.append("")
            lines.append(f"- Jargon level: `{guide['jargon_level']}`")
            lines.append(f"- Word target: `{guide['word_target']}`")
            lines.append(f"- First-line target: `{guide['first_line_target']}`")
            lines.append(f"- Tone: {guide['tone']}")
            lines.append(f"- Notes: {guide['notes']}")
            lines.append("")
        lines.append("## Worked Example")
        lines.append("")
        lines.append("Raw approved context:")
        lines.append("- `7 Layer is active across infrastructure consulting, cloud migration, and vulnerability assessment.`")
        lines.append("")
        lines.append("Too long / too explanatory version:")
        lines.append("- `7 Layer is deep in infra, cloud and vulnerability assessment work which increases risk across shared tools, handoffs and client side management.`")
        lines.append("")
        lines.append("Why it still needs editing:")
        lines.append("- It is more conversational than the earlier draft, but it is not actually shorter.")
        lines.append("- It tries to do first line, diagnosis, product, and CTA all in one breath.")
        lines.append("- The first line should be separated from the body.")
        lines.append("")
        lines.append("Leadership version:")
        lines.append("```txt")
        lines.append("7 Layer is active across infra, cloud, and vuln work.")
        lines.append("")
        lines.append("That usually means more client-side drift can surface late across shared tools and delivery paths.")
        lines.append("We monitor those file changes without adding agent sprawl.")
        lines.append("Mind if I send the short version?")
        lines.append("```")
        lines.append("")
        lines.append("IT Ops version:")
        lines.append("```txt")
        lines.append("7 Layer is deep in infra, cloud, and vuln work.")
        lines.append("")
        lines.append("Usually that means more shared tools, handoffs, and change paths to keep visible.")
        lines.append("We monitor drift on the files that matter without adding another heavy deployment.")
        lines.append("Worth a quick outline?")
        lines.append("```")
        lines.append("")
        lines.append("Security / Compliance version:")
        lines.append("```txt")
        lines.append("7 Layer is active across infra, cloud, and vulnerability work.")
        lines.append("")
        lines.append("That usually leaves more evidence-sensitive paths and more room for quiet drift.")
        lines.append("We flag file changes early so the trail is cleaner when someone needs answers.")
        lines.append("Open to the short version?")
        lines.append("```")
        lines.append("")
        lines.append("The key is that the final copy should keep the signal, but compress the wording and split the logic into clean, human sentences.")
        lines.append("")
        return "\n".join(lines)

    def _build_copy_controls(self, *, campaign_profile: str) -> dict[str, Any]:
        return {
            "campaign_profile": campaign_profile,
            "global": {
                "first_line_target_words_min": 8,
                "first_line_target_words_max": 14,
                "first_line_hard_max_words": 16,
                "email_target_words_min": 45,
                "email_target_words_max": 85,
                "sentence_count_target_min": 3,
                "sentence_count_target_max": 5,
                "tone": "conversational",
                "style_goal": "sound like a clear text or note from a real operator, not an LLM summary",
                "disallow_raw_social_copy": True,
                "require_approved_signal_only": True,
                "review_questions": [
                    "Did this actually get shorter?",
                    "Does the first line read naturally out loud?",
                    "Does the email sound conversational and organic?",
                    "Is jargon dialed appropriately for the role segment?",
                    "Can any sentence be split into two cleaner ones?",
                ],
            },
            "segments": SEGMENT_STYLE_GUIDES,
        }

    def _render_review_html(
        self,
        *,
        seller: str,
        campaign_profile: str,
        reviewed_people_count: int,
        company_count: int,
        signal_rows: list[SignalReviewRow],
        segment_rows: list[SegmentReviewRow],
        signal_counts: dict[str, int],
        segment_counts: dict[str, int],
    ) -> str:
        def esc(value: str) -> str:
            return html.escape(value or "")

        def card(title: str, body: str) -> str:
            return (
                '<section class="card">'
                f"<h3>{esc(title)}</h3>"
                f"{body}"
                "</section>"
            )

        def ul(items: list[str]) -> str:
            return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"

        def sample_signals(disposition: str, limit: int) -> list[SignalReviewRow]:
            return [row for row in signal_rows if row.signal_disposition == disposition][:limit]

        def sample_segments(disposition: str, limit: int) -> list[SegmentReviewRow]:
            return [row for row in segment_rows if row.segment_disposition == disposition][:limit]

        top_roles_by_segment: dict[str, list[str]] = {}
        for segment in SEGMENT_CONFIG:
            roles: list[str] = []
            for row in segment_rows:
                if row.suggested_segment == segment and row.role_title and row.role_title not in roles:
                    roles.append(row.role_title)
                if len(roles) >= 6:
                    break
            top_roles_by_segment[segment] = roles

        signal_cards = []
        for disposition in ("keep", "review", "drop"):
            rows = sample_signals(disposition, 8)
            items = [
                f"{row.company_name}: {row.cleaned_signal or row.raw_signal} ({row.signal_reason})"
                for row in rows
            ]
            signal_cards.append(
                card(
                    f"{disposition.title()} Signals ({signal_counts.get(disposition, 0)})",
                    ul(items) if items else "<p>No examples.</p>",
                )
            )

        segment_cards = []
        for disposition in ("reclassify", "review", "drop"):
            rows = sample_segments(disposition, 10)
            items = [
                f"{row.company_name}: {row.role_title} -> {row.suggested_segment} ({row.segment_reason})"
                for row in rows
            ]
            segment_cards.append(
                card(
                    f"{disposition.title()} Roles",
                    ul(items) if items else "<p>No examples.</p>",
                )
            )

        sequence_cards = []
        for segment_key in ("leadership", "it_ops", "security_compliance", "commercial_client", "admin_legal"):
            count = segment_counts.get(segment_key, 0)
            if count == 0:
                continue
            config = SEGMENT_CONFIG[segment_key]
            body = (
                f"<p><strong>Rows:</strong> {count}</p>"
                f"<p><strong>Recommended:</strong> {esc(config['recommended'])}</p>"
                f"<p><strong>Why:</strong> {esc(config['why'])}</p>"
                f"<p><strong>Example roles:</strong> {esc(', '.join(top_roles_by_segment.get(segment_key, [])) or 'n/a')}</p>"
                f"<p><strong>Value rotation:</strong> {esc(config['value_rotation'])}</p>"
                '<div class="option-grid">'
                f'<div class="option"><h4>Option 1</h4><p><strong>Strategy:</strong> {esc(config["recommended"])}</p><p><strong>Subject:</strong> <code>{esc(config["option_1_subject"])}</code></p><p>{esc(config["option_1_first_line"])}</p></div>'
                f'<div class="option"><h4>Option 2</h4><p><strong>Strategy:</strong> Billboard</p><p><strong>Subject:</strong> <code>{esc(config["option_2_subject"])}</code></p><p>{esc(config["option_2_first_line"])}</p></div>'
                f'<div class="option"><h4>Option 3</h4><p><strong>Strategy:</strong> AI Generic</p><p><strong>Subject:</strong> <code>{esc(config["option_3_subject"])}</code></p><p>{esc(config["option_3_first_line"])}</p></div>'
                "</div>"
            )
            sequence_cards.append(card(config["display"], body))

        html_parts = [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            f"<title>{esc(seller.title())} Campaign Review</title>",
            "<style>",
            """
            :root { color-scheme: light; --bg:#f5f1e8; --paper:#fffdf8; --ink:#1d1d1b; --muted:#645f55; --line:#d8d0c2; --accent:#c66a13; --accent2:#264653; --good:#2f6f44; --warn:#a05a00; --bad:#9b2226; }
            * { box-sizing:border-box; }
            body { margin:0; font-family: Georgia, 'Iowan Old Style', serif; background:linear-gradient(180deg,#efe7d7 0%, #f8f5ee 100%); color:var(--ink); }
            .shell { max-width: 1240px; margin: 0 auto; padding: 32px 20px 56px; }
            .hero { background:var(--paper); border:1px solid var(--line); border-radius:20px; padding:28px 30px; box-shadow:0 12px 28px rgba(0,0,0,.05); }
            .eyebrow { text-transform:uppercase; letter-spacing:.12em; font:600 12px/1.2 ui-sans-serif,system-ui,sans-serif; color:var(--accent); margin:0 0 12px; }
            h1,h2,h3,h4 { margin:0 0 12px; font-weight:700; }
            h1 { font-size:38px; line-height:1.05; }
            h2 { font-size:24px; margin-top:28px; }
            h3 { font-size:20px; }
            h4 { font-size:16px; }
            p, li { font-size:15px; line-height:1.55; }
            .muted { color:var(--muted); }
            .stats { display:grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap:14px; margin-top:18px; }
            .stat { background:#faf6ed; border:1px solid var(--line); border-radius:16px; padding:14px 16px; }
            .stat-label { display:block; font:600 12px/1.2 ui-sans-serif,system-ui,sans-serif; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin-bottom:8px; }
            .stat-value { font:700 28px/1 ui-sans-serif,system-ui,sans-serif; color:var(--accent2); }
            .section { margin-top:24px; }
            .grid { display:grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap:16px; }
            .grid-3 { display:grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap:16px; }
            .card { background:var(--paper); border:1px solid var(--line); border-radius:18px; padding:18px 18px 16px; box-shadow:0 8px 24px rgba(0,0,0,.04); }
            .badge { display:inline-block; margin-right:8px; margin-bottom:8px; padding:5px 10px; border-radius:999px; background:#f3ecde; border:1px solid var(--line); font:600 12px/1 ui-sans-serif,system-ui,sans-serif; color:var(--muted); }
            .option-grid { display:grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap:12px; margin-top:14px; }
            .option { background:#faf6ed; border:1px solid var(--line); border-radius:14px; padding:12px; }
            code { background:#f3ecde; padding:2px 6px; border-radius:6px; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; }
            ul { margin: 0; padding-left: 18px; }
            .callout { border-left:4px solid var(--accent); padding:10px 14px; background:#fff7ea; border-radius:8px; }
            .good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
            @media (max-width: 980px) { .stats,.grid,.grid-3,.option-grid { grid-template-columns:1fr; } h1 { font-size:30px; } }
            """,
            "</style>",
            "</head>",
            "<body>",
            '<main class="shell">',
            '<section class="hero">',
            '<div class="eyebrow">Campaign Review Flow</div>',
            f"<h1>{esc(seller.title())} MSP/MSSP Copy Review</h1>",
            '<p class="muted">This review stage is the approval gate before final spintax generation. It is designed to stop bad signal injection, expose role reclassification, and let you approve sequence strategy by segment.</p>',
            '<div class="callout"><strong>Incorrect context injection status:</strong> partially fixed. The junk signal problem is now isolated and visible in review. Final copy generation still needs to consume only approved signals from this stage before the issue is fully closed.</div>',
            '<div class="stats">',
            f'<div class="stat"><span class="stat-label">Campaign Profile</span><span class="stat-value">{esc(campaign_profile)}</span></div>',
            f'<div class="stat"><span class="stat-label">Qualified People</span><span class="stat-value">{reviewed_people_count}</span></div>',
            f'<div class="stat"><span class="stat-label">Companies</span><span class="stat-value">{company_count}</span></div>',
            f'<div class="stat"><span class="stat-label">Signals Reviewed</span><span class="stat-value">{sum(signal_counts.values())}</span></div>',
            "</div>",
            "</section>",
            '<section class="section">',
            "<h2>Step 1. Campaign Direction</h2>",
            '<div class="grid">',
            card("Target Audience", "<p>MSP and MSSP operators, security owners, commercial/client owners, and coordination-heavy roles close to delivery proof.</p>"),
            card("Core Pain Point", "<p>Quiet change drift, evidence gaps, alert noise, client-environment complexity, and client-facing follow-up when teams cannot explain what changed.</p>"),
            card("Value Proposition", "<p>Agentless visibility across client paths, shared tooling, and evidence-sensitive file flows before quiet drift becomes delivery, review, or renewal pain.</p>"),
            card("Proof Point Direction", "<p>Company-site research, LinkedIn company activity, and operating signals tied to delivery, security, compliance, onboarding, or expansion.</p>"),
            "</div>",
            "</section>",
            '<section class="section">',
            "<h2>Step 1A. Signal Hygiene Review</h2>",
            '<div class="grid-3">' + "".join(signal_cards) + "</div>",
            "</section>",
            '<section class="section">',
            "<h2>Step 1B. Segment Review</h2>",
            '<div class="grid">',
            card(
                "Segment Mix",
                ul([
                    f"{SEGMENT_CONFIG.get(key, {}).get('display', key)}: {count}"
                    for key, count in segment_counts.items()
                ]),
            ),
            card(
                "Review Guidance",
                ul([
                    "Approve reclassify rows that better match the real buyer motion.",
                    "Drop recruiting, payroll, logistics, and obvious non-buyer roles.",
                    "Keep technical roles with IT Ops, legal/compliance with Security / Compliance, and client-facing roles with Commercial / Client.",
                ]),
            ),
            "</div>",
            '<div class="grid-3">' + "".join(segment_cards) + "</div>",
            "</section>",
            '<section class="section">',
            "<h2>Step 2. Sequence Strategy Review</h2>",
            '<p class="muted">These are strategy defaults, not final emails. Approve one default path per segment before spintax is regenerated.</p>',
            '<div class="grid">' + "".join(sequence_cards) + "</div>",
            "</section>",
            '<section class="section">',
            "<h2>Step 3. Copy Style Review</h2>",
            '<div class="grid">',
            card(
                "What The Final Copy Should Feel Like",
                ul([
                    "Easy to read, clear, and conversational.",
                    "Closer to a text or direct note than a polished marketing email.",
                    "Segment-aware on technical framing without sounding robotic.",
                    "Signal-led, but only from approved context.",
                ]),
            ),
            card(
                "Questions To Ask While Editing",
                ul([
                    "Did this actually get shorter?",
                    "Does it sound more organic?",
                    "Is the jargon level right for the segment?",
                    "Can the first line be 10 words or fewer?",
                    "Can one long sentence become two shorter ones?",
                ]),
            ),
            "</div>",
            '<div class="grid-3">' + "".join(
                card(
                    SEGMENT_CONFIG[key]["display"],
                    (
                        f"<p><strong>Jargon:</strong> {esc(SEGMENT_STYLE_GUIDES[key]['jargon_level'])}</p>"
                        f"<p><strong>Word target:</strong> {esc(SEGMENT_STYLE_GUIDES[key]['word_target'])}</p>"
                        f"<p><strong>First line:</strong> {esc(SEGMENT_STYLE_GUIDES[key]['first_line_target'])}</p>"
                        f"<p><strong>Tone:</strong> {esc(SEGMENT_STYLE_GUIDES[key]['tone'])}</p>"
                        f"<p>{esc(SEGMENT_STYLE_GUIDES[key]['notes'])}</p>"
                    ),
                )
                for key in ("leadership", "it_ops", "security_compliance", "commercial_client", "admin_legal")
            ) + "</div>",
            card(
                "Worked Example",
                (
                    "<p><strong>Approved context:</strong> <code>7 Layer is active across infrastructure consulting, cloud migration, and vulnerability assessment.</code></p>"
                    "<p><strong>What changed:</strong> the better version is more conversational, but it still needs to be shorter and split into clean sentences.</p>"
                    "<p><strong>Leadership version:</strong></p>"
                    "<pre>7 Layer is active across infra, cloud, and vuln work.\n\nThat usually means more client-side drift can surface late across shared tools and delivery paths.\nWe monitor those file changes without adding agent sprawl.\nMind if I send the short version?</pre>"
                    "<p><strong>IT Ops version:</strong></p>"
                    "<pre>7 Layer is deep in infra, cloud, and vuln work.\n\nUsually that means more shared tools, handoffs, and change paths to keep visible.\nWe monitor drift on the files that matter without adding another heavy deployment.\nWorth a quick outline?</pre>"
                    "<p><strong>Security / Compliance version:</strong></p>"
                    "<pre>7 Layer is active across infra, cloud, and vulnerability work.\n\nThat usually leaves more evidence-sensitive paths and more room for quiet drift.\nWe flag file changes early so the trail is cleaner when someone needs answers.\nOpen to the short version?</pre>"
                ),
            ),
            "</section>",
            '<section class="section">',
            "<h2>How To Review The Flow</h2>",
            '<div class="grid">',
            card(
                "Current Stage",
                ul([
                    "Use this HTML to approve signal hygiene, segment mapping, and default sequence approach.",
                    "Use the CSVs when you want row-level detail or bulk cleanup.",
                    "No final copy should be trusted until approved signals replace raw company signals in materialization.",
                ]),
            ),
            card(
                "Next Stage",
                ul([
                    "After signoff, regenerate segment-level spintax from approved rules only.",
                    "Render a new spintax HTML preview for the actual sequence copy.",
                    "Then materialize the final people CSV and send-ready CSV.",
                ]),
            ),
            "</div>",
            "</section>",
            "</main>",
            "</body>",
            "</html>",
        ]
        return "\n".join(html_parts)
