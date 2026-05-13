from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from listbuild.company_names import (
    CompanyNameNormalization,
    MiniMaxCompanyNameNormalizer,
    heuristic_normalize_company_name,
    needs_llm_company_name_cleanup,
)
from listbuild.datasets import PersonRow, person_quality_score, write_person_rows_csv
from listbuild.providers import MiniMaxClient
from listbuild.runs import build_run_paths, scaffold_run_directories
from listbuild.spam_guard import (
    BANNED_SINGLE_WORDS,
    assert_safe_subject_spintax,
    assert_spam_safe,
    extract_spintax_variants,
    find_spam_violations,
    spam_safe_company_name,
)


VOICE_FAMILY_BY_BUCKET = {
    "technical_it": "technical_it",
    "finance": "finance",
    "executive_owner": "executive_owner",
    "operations": "operations",
    "security_compliance_risk": "security_compliance_risk",
    "leadership": "leadership",
    "it_ops": "it_ops",
    "security": "security",
    "commercial": "commercial",
    "admin": "admin",
    "legal": "security_compliance_risk",
    "clinical_admin": "operations",
    "hr_admin": "operations",
    "generic_management": "executive_owner",
}

BUCKET_DESCRIPTIONS = {
    "technical_it": "Technical / IT",
    "finance": "Finance",
    "executive_owner": "Executive / Owner",
    "operations": "Operations",
    "security_compliance_risk": "Security / Compliance / Risk",
    "leadership": "Leadership",
    "it_ops": "IT Operations",
    "security": "Security",
    "commercial": "Commercial",
    "admin": "Administrative",
    "legal": "Legal",
    "clinical_admin": "Clinical / Admin",
    "hr_admin": "HR / Admin",
    "generic_management": "General Management",
}

REPORT_ROLE_NOTES = {
    "technical_it": "Technical voice. Keep the pitch lightweight, concrete, and deployment-aware. Mention hash-based monitoring, webhook alerts, no agents, and EHR / reporting file coverage.",
    "finance": "Finance voice. Land on compliance exposure, settlement math, board-readiness, and under-resourced teams without sounding alarmist.",
    "executive_owner": "Executive voice. Focus on mission continuity, reputation, funding exposure, and avoiding quiet issues that become board-level problems.",
    "operations": "Operations voice. Emphasize multi-site complexity, staff not chasing issues host by host, and reducing operational drag across clinics.",
    "security_compliance_risk": "Compliance voice. Use audit-readiness, defensible evidence, and control-gap framing. Keep it calm and credible rather than fear-heavy.",
    "leadership": "Leadership voice. Focus on client trust, scaling service delivery, quiet operational risk, and visibility across client environments.",
    "it_ops": "IT ops voice. Focus on change visibility, shared tools, client environments, noisy escalations, and staying agentless where possible.",
    "security": "Security voice. Focus on evidence, alert noise, review readiness, client security operations, and defensible trails without tool sprawl.",
    "commercial": "Commercial voice. Focus on client trust, renewals, proof of delivery quality, and reducing surprises that account teams end up explaining.",
    "admin": "Administrative voice. Focus on follow-up drag, evidence chase, coordination load, and reducing manual back-and-forth.",
}

CAMPAIGN_PROFILE_AUTO = "auto"
CAMPAIGN_PROFILE_FQHC = "fqhc"
CAMPAIGN_PROFILE_MSP_MSSP = "msp_mssp"
CAMPAIGN_PROFILE_GENERIC = "generic"

MSP_MSSP_BUCKET_MAP = {
    "leadership": "leadership",
    "executive_owner": "leadership",
    "generic_management": "leadership",
    "it_ops": "it_ops",
    "technical_it": "it_ops",
    "security": "security",
    "security_compliance_risk": "security",
    "legal": "security",
    "commercial": "commercial",
    "finance": "leadership",
    "admin": "admin",
    "operations": "admin",
    "hr_admin": "admin",
    "clinical_admin": "admin",
}


def _is_truthy(value: str) -> bool:
    return _clean_text(value).lower() in {"true", "yes", "1", "y"}


@dataclass(frozen=True, slots=True)
class CampaignPrepPolicy:
    people_csv: str
    email_only: bool = True
    use_minimax_name_normalization: bool = False
    use_minimax_personalization: bool = False
    max_people: int = 0
    campaign_profile: str = CAMPAIGN_PROFILE_AUTO


@dataclass(frozen=True, slots=True)
class CampaignPrepRun:
    run_slug: str
    people_csv: str
    send_ready_csv: str
    companies_csv: str
    report_md: str
    summary_json: str
    row_count: int
    company_count: int
    bucket_counts: dict[str, int]
    campaign_profile: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "people_csv": self.people_csv,
            "send_ready_csv": self.send_ready_csv,
            "companies_csv": self.companies_csv,
            "report_md": self.report_md,
            "summary_json": self.summary_json,
            "row_count": self.row_count,
            "company_count": self.company_count,
            "bucket_counts": self.bucket_counts,
            "campaign_profile": self.campaign_profile,
        }


@dataclass(frozen=True, slots=True)
class RoleBucketCopySet:
    bucket: str
    audience: str
    voice_family: str
    role_note: str
    s1: str
    s2: str
    s3: str
    s4: str
    e1: str
    e2: str
    e3: str
    e4: str


@dataclass(frozen=True, slots=True)
class MiniMaxPersonalizationResult:
    p1: str
    raw_response: dict[str, Any]
    parsed_response: dict[str, Any]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def _render_body_template(
    template: str,
    *,
    first_name: str,
    personalization_line: str,
) -> str:
    salutation = f"{first_name.strip()},\n\n" if first_name.strip() else ""
    rendered = (
        (template or "")
        .replace("{{first_name}},\n\n", salutation)
        .replace("{{first_name}},", first_name.strip() + "," if first_name.strip() else "")
        .replace("{{first_name}}", first_name.strip())
        .replace("{{personalization_line}}", personalization_line.strip())
    )
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered


def _render_subject_variant(
    template: str,
    *,
    row_seed: str,
    slot: str,
) -> str:
    variants = extract_spintax_variants(template)
    if not variants:
        return ""
    if len(variants) == 1:
        return variants[0]
    seed_material = f"{row_seed}|{slot}|{template}"
    digest = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(variants)
    return variants[index]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence_match is not None:
        try:
            payload = json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return ""


def _truncate(value: str, limit: int) -> str:
    collapsed = _clean_text(value)
    if len(collapsed) <= limit:
        return collapsed
    shortened = collapsed[: limit - 3].rstrip(" ,;:-")
    return f"{shortened}..."


def _split_fragments(value: str) -> list[str]:
    if not value.strip():
        return []
    return [_clean_text(part) for part in value.split("|") if _clean_text(part)]


def _first_sentence(value: str, *, limit: int = 320) -> str:
    collapsed = _clean_text(value)
    if not collapsed:
        return ""
    for divider in (". ", "; ", " - ", " plus ", ", plus ", ", and "):
        if divider in collapsed:
            collapsed = collapsed.split(divider, 1)[0]
            break
    return _truncate(collapsed.strip(" ."), limit)


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in full_name.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _normalized_bucket(value: str) -> str:
    bucket = _clean_text(value).lower()
    return bucket or "generic_management"


def _resolve_campaign_profile(
    *,
    policy_value: str,
    seller: str,
    segment: str,
) -> str:
    profile = _clean_text(policy_value).lower()
    if profile and profile != CAMPAIGN_PROFILE_AUTO:
        return profile
    haystack = " ".join((_clean_text(seller), _clean_text(segment))).lower()
    if "msp" in haystack or "mssp" in haystack:
        return CAMPAIGN_PROFILE_MSP_MSSP
    if "fqhc" in haystack:
        return CAMPAIGN_PROFILE_FQHC
    return CAMPAIGN_PROFILE_GENERIC


def _bucket_tokens(value: str) -> list[str]:
    cleaned = _clean_text(value).lower()
    if not cleaned:
        return []
    tokens = re.split(r"[|,/]", cleaned)
    return [token.strip() for token in tokens if token.strip()]


def _msp_bucket_from_title(title: str) -> str:
    lowered = _clean_text(title).casefold()
    if not lowered:
        return ""
    if any(
        re.search(pattern, lowered)
        for pattern in (
            r"\bceo\b",
            r"\bpresident\b",
            r"\bowner\b",
            r"\bfounder\b",
            r"\bco[- ]founder\b",
            r"\bcoo\b",
            r"\bcfo\b",
            r"\bcmo\b",
            r"\bcio\b",
            r"\bcto\b",
            r"\bchief\b",
            r"\bmanaging director\b",
            r"\bboard member\b",
            r"\bvice president\b",
            r"\bsvp\b",
            r"\bevp\b",
            r"\bvp\b",
        )
    ):
        return "leadership"
    if any(
        re.search(pattern, lowered)
        for pattern in (
            r"\bsecurity\b",
            r"\bsoc\b",
            r"\bciso\b",
            r"\bcompliance\b",
            r"\brisk\b",
            r"\blegal\b",
            r"\bprivacy\b",
            r"\bgrc\b",
            r"\binformation assurance\b",
            r"\bblue team\b",
            r"\bthreat\b",
        )
    ):
        return "security"
    if any(
        term in lowered
        for term in (
            "account manager",
            "business development",
            "sales",
            "channel",
            "partner",
            "client advisor",
            "customer success",
            "commercial",
            "revenue",
            "growth",
            "account executive",
        )
    ):
        return "commercial"
    if any(
        term in lowered
        for term in (
            "assistant",
            "office manager",
            "administrative",
            "coordinator",
            "executive assistant",
        )
    ):
        return "admin"
    if any(
        term in lowered
        for term in (
            "technical consulting",
            "technical consultant",
            "technical services",
            "service delivery",
            "managed services",
            "systems engineer",
            "system engineer",
            "system administrator",
            "it manager",
            "it operations",
            "infrastructure",
            "network",
            "solutions architect",
            "solution architect",
            "technical director",
            "director of technical",
            "director, technical",
            "engineering",
            "consulting",
            "operations",
            "delivery",
            "architect",
        )
    ):
        return "it_ops"
    return ""


def _campaign_bucket(row: dict[str, str], *, campaign_profile: str) -> str:
    candidate_values = [
        row.get("selection_bucket") or "",
        row.get("primary_matched_role_segment") or "",
        row.get("source_role_segment") or "",
        row.get("source_role_segments") or "",
        row.get("matched_role_segments") or "",
    ]
    normalized_tokens: list[str] = []
    for candidate in candidate_values:
        normalized_tokens.extend(_bucket_tokens(candidate))

    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        title_bucket = _msp_bucket_from_title(_role_title(row))
        for token in normalized_tokens:
            mapped = MSP_MSSP_BUCKET_MAP.get(token)
            if mapped:
                if mapped == "leadership" and title_bucket and title_bucket != "leadership":
                    return title_bucket
                return mapped
        if title_bucket:
            return title_bucket
        return "it_ops"

    if normalized_tokens:
        return _normalized_bucket(normalized_tokens[0])
    return "generic_management"


def _role_title(row: dict[str, str]) -> str:
    for key in (
        "role_title",
        "prospeo_current_job_title",
        "corpus_parsed_title",
        "harvest_headline",
        "primary_matched_title",
        "matched_titles",
    ):
        candidate = _clean_text(row.get(key))
        if not candidate:
            continue
        if "|" in candidate:
            candidate = candidate.split("|", 1)[0].strip()
        candidate_lower = candidate.lower()
        for splitter in (" at ", " @ ", " | ", " - ", " – "):
            if splitter in candidate_lower:
                index = candidate_lower.find(splitter)
                if index > 0:
                    candidate = candidate[:index]
                    break
        if " of " in candidate_lower:
            company_name = _clean_text(row.get("company_name")).lower()
            company_copy_name = _clean_text(row.get("company_copy_name")).lower()
            if company_name and f" of {company_name}" in candidate_lower:
                candidate = candidate[: candidate_lower.find(" of ")].strip()
            elif company_copy_name and f" of {company_copy_name}" in candidate_lower:
                candidate = candidate[: candidate_lower.find(" of ")].strip()
        return candidate.strip(" ,")
    return ""


def _role_description(row: dict[str, str]) -> str:
    about = _clean_text(row.get("harvest_about"))
    if about:
        first = _first_sentence(about, limit=420)
        if len(first) >= 30:
            return _truncate(first, 420)
        return _truncate(about, 420)
    posts = _clean_text(row.get("harvest_recent_posts_summary"))
    if posts:
        return _truncate(posts, 420)
    return _truncate(_clean_text(row.get("company_painpoint")), 420)


def _company_signal_phrase(row: dict[str, str]) -> str:
    fragments = _split_fragments(_clean_text(row.get("company_signals")))
    if fragments:
        return fragments[0]
    description = _clean_text(row.get("company_description"))
    if description:
        return _first_sentence(description, limit=160)
    return "has a lot moving across sites"


def _company_offer_phrase(row: dict[str, str]) -> str:
    value = _clean_text(row.get("company_offer"))
    if value:
        return _first_sentence(value, limit=170)
    description = _clean_text(row.get("company_description"))
    if description:
        return _first_sentence(description, limit=170)
    return "serves a broad patient population across multiple clinics"


def _company_icp_phrase(row: dict[str, str]) -> str:
    value = _clean_text(row.get("company_icp"))
    if value:
        return _first_sentence(value, limit=170)
    return "patients who rely on the clinic staying operational and audit-ready"


def _company_painpoint_phrase(row: dict[str, str]) -> str:
    value = _clean_text(row.get("company_painpoint"))
    if value:
        return _first_sentence(value, limit=180)
    return "quiet file changes turning into a compliance issue"


def _row_looks_like_fqhc(row: dict[str, str]) -> bool:
    company_name = (
        _clean_text(row.get("prospeo_company_name"))
        or _clean_text(row.get("source_company_name"))
        or _clean_text(row.get("company_name"))
    ).lower()
    haystack = " ".join(
        filter(
            None,
            [
                _clean_text(row.get("source_company_tier")),
                _clean_text(row.get("company_description")),
                _clean_text(row.get("company_offer")),
                _clean_text(row.get("company_icp")),
                _clean_text(row.get("company_painpoint")),
                _clean_text(row.get("company_signals")),
            ],
        )
    ).lower()
    negative_hints = (
        "not applicable -",
        "not a healthcare organization",
        "recruiting",
        "staffing",
        "environmental instrumentation",
        "private equity",
        "professional services",
        "health care foundation",
        "revenue cycle management",
        "claims denial",
        "billing and claims",
        "clearinghouse",
        "ehr services",
        "consulting",
        "med tech solutions",
        "targets community health centers",
    )
    if any(hint in haystack for hint in negative_hints):
        return False
    positive_hints = (
        "federally qualified health center",
        "fqhc",
        "community health center",
        "community health centers",
        "health center",
        "clinic",
        "medical center",
        "rural health",
        "family health",
        "health services center",
    )
    if not any(hint in haystack for hint in positive_hints):
        return False
    provider_name_hints = (
        "health",
        "clinic",
        "medical",
        "care",
        "center",
        "family",
        "community",
        "dental",
        "wellness",
    )
    return any(hint in company_name for hint in provider_name_hints)


def _row_looks_like_msp_mssp(row: dict[str, str]) -> bool:
    haystack = " ".join(
        filter(
            None,
            [
                _clean_text(row.get("company_name")),
                _clean_text(row.get("company_description")),
                _clean_text(row.get("company_offer")),
                _clean_text(row.get("company_icp")),
                _clean_text(row.get("company_painpoint")),
                _clean_text(row.get("company_signals")),
                _clean_text(row.get("company_recent_post_summary")),
            ],
        )
    ).lower()
    if any(hint in haystack for hint in MSP_MSSP_NEGATIVE_HINTS):
        return False
    if any(hint in haystack for hint in MSP_MSSP_STRONG_POSITIVE_HINTS):
        return True
    weak_hits = sum(1 for hint in MSP_MSSP_WEAK_POSITIVE_HINTS if hint in haystack)
    return weak_hits >= 2


def _row_matches_campaign_profile(row: dict[str, str], *, campaign_profile: str) -> bool:
    if campaign_profile == CAMPAIGN_PROFILE_FQHC:
        return _row_looks_like_fqhc(row)
    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        return _row_looks_like_msp_mssp(row)
    return True


def _context_text(row: dict[str, str]) -> str:
    return " ".join(
        filter(
            None,
            [
                _clean_text(row.get("company_description")),
                _clean_text(row.get("company_offer")),
                _clean_text(row.get("company_icp")),
                _clean_text(row.get("company_painpoint")),
                _clean_text(row.get("company_signals")),
                _clean_text(row.get("company_signal_sources")),
                _clean_text(row.get("company_recent_post_summary")),
            ],
        )
    ).casefold()


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


ROLE_PRIORITY_HINTS = {
    "technical_it": (
        "ehr",
        "portal",
        "cloud",
        "implementation",
        "medfusion",
        "healow",
        "athena",
        "eclinicalworks",
        "ecw",
        "report",
        "export",
        "shared",
        "integration",
        "workflow",
    ),
    "finance": (
        "ocr",
        "audit",
        "340b",
        "expansion",
        "encounters",
        "employees",
        "locations",
        "funding",
        "compliance",
        "review",
        "cost",
    ),
    "operations": (
        "locations",
        "mobile",
        "service line",
        "counties",
        "expansion",
        "workflow",
        "operations",
        "hiring",
        "report",
        "shared",
    ),
    "executive_owner": (
        "locations",
        "mobile",
        "expansion",
        "encounters",
        "partnership",
        "launch",
        "growth",
        "service line",
        "community",
        "patient",
    ),
    "security_compliance_risk": (
        "audit",
        "ocr",
        "review",
        "compliance",
        "integrity",
        "file",
        "trail",
        "report",
        "control",
    ),
    "leadership": (
        "managed services",
        "client",
        "growth",
        "service delivery",
        "renewal",
        "security",
        "operations",
        "visibility",
        "compliance",
        "risk",
    ),
    "it_ops": (
        "managed services",
        "it support",
        "help desk",
        "cloud",
        "backup",
        "network",
        "client",
        "infrastructure",
        "ticket",
        "change",
    ),
    "security": (
        "security",
        "cyber",
        "soc",
        "siem",
        "mdr",
        "xdr",
        "alert",
        "incident",
        "compliance",
        "evidence",
    ),
    "commercial": (
        "client",
        "customer",
        "partner",
        "channel",
        "renewal",
        "service delivery",
        "trust",
        "account",
        "growth",
        "security",
    ),
    "admin": (
        "review",
        "evidence",
        "coordination",
        "follow-up",
        "client",
        "operations",
        "compliance",
        "documentation",
        "support",
        "handoff",
    ),
}

SERVICE_TERMS = (
    ("primary care", "primary care"),
    ("dental", "dental"),
    ("behavioral health", "behavioral health"),
    ("mental health", "behavioral health"),
    ("pediatrics", "pediatrics"),
    ("telehealth", "telehealth"),
    ("pharmacy", "pharmacy"),
    ("optometry", "optometry"),
    ("specialty", "specialty care"),
    ("mobile unit", "mobile units"),
    ("mobile clinic", "mobile units"),
)

MSP_SERVICE_TERMS = (
    ("managed services", "managed services"),
    ("managed it", "managed IT"),
    ("managed security", "managed security"),
    ("msp", "managed IT"),
    ("mssp", "managed security"),
    ("it infrastructure", "infra"),
    ("infrastructure", "infra"),
    ("it support", "IT support"),
    ("help desk", "help desk"),
    ("cloud migration", "cloud migration"),
    ("cloud", "cloud services"),
    ("vulnerability assessment", "vulnerability assessment"),
    ("vulnerability management", "vulnerability work"),
    ("backup", "backup and disaster recovery"),
    ("disaster recovery", "backup and disaster recovery"),
    ("network", "network operations"),
    ("cybersecurity", "cybersecurity services"),
    ("cyber security", "cybersecurity services"),
    ("soc", "SOC services"),
    ("siem", "SIEM coverage"),
    ("mdr", "MDR"),
    ("xdr", "XDR"),
    ("compliance", "compliance services"),
    ("consulting", "IT consulting"),
    ("vcio", "vCIO"),
    ("vciso", "vCISO"),
)

SKIP_SIGNAL_PHRASES = (
    "no recent news",
    "no linkedin activity",
    "fqhc status confirmed",
)

WEAK_SIGNAL_PATTERNS = (
    "g2 high performer",
    "high performer",
    "recognized as",
    "recognized by",
    "award",
    "awards",
    "excited to share",
    "proud to share",
    "webinar",
    "podcast",
    "conference",
    "summit",
    "event",
)


def _contains_weak_signal_pattern(text: str) -> bool:
    lowered = text.casefold()
    for pattern in WEAK_SIGNAL_PATTERNS:
        if re.search(rf"\b{re.escape(pattern)}\b", lowered):
            return True
    return False

MSP_MSSP_NEGATIVE_HINTS = (
    "staffing",
    "recruitment",
    "recruiting",
    "payroll",
    "contingent workforce",
    "talent acquisition",
    "staff augmentation",
    "executive search",
    "intellectual property",
    "trademark",
    "brand protection",
    "domain management",
    "domain portfolio",
    "registrar",
    "workforce solutions",
    "medical devices",
    "life sciences sector is growing fast",
    "value-added distributor",
    "value added distributor",
    "intermediary distributor",
    "reseller channels",
    "vendor alliances",
    "channel coverage",
)

MSP_MSSP_STRONG_POSITIVE_HINTS = (
    "msp",
    "mssp",
    "managed services",
    "managed it",
    "managed security",
    "it support",
    "help desk",
    "service desk",
    "outsourced it",
    "cloud services",
    "cloud migration",
    "backup and disaster recovery",
    "managed detection and response",
    "security operations center",
    "socaas",
    "soc as a service",
    "vcio",
    "vciso",
)

MSP_MSSP_WEAK_POSITIVE_HINTS = (
    "cybersecurity",
    "cyber security",
    "soc",
    "siem",
    "mdr",
    "xdr",
    "edr",
    "ueba",
    "soar",
    "network",
    "infrastructure",
    "endpoint",
    "monitoring",
)

INDUSTRY_SNIPPETS = (
    "private equity",
    "financial services",
    "manufacturing",
    "distribution",
    "professional services",
    "government",
    "not-for-profit",
    "public sector",
    "education",
    "healthcare",
    "industrial",
    "legal",
    "canadian mid-market",
    "mid-market",
    "enterprise",
)


def _sanitize_fragment_for_copy(value: str, *, limit: int = 140) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"#\w+", " ", text)
    text = re.sub(r"[\u2600-\u27BF\U0001F300-\U0001FAFF]", " ", text)
    text = re.sub(
        r"^(?:we(?:'re| are)\s+(?:excited|proud)\s+to\s+(?:share|announce)\s+that|(?:excited|proud)\s+to\s+(?:share|announce)\s+that)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\([^)]*\)", "", text)
    text = text.replace("|", " ").replace("/", " / ")
    text = re.sub(r"\s+", " ", text).strip(" ,;:-")
    text = re.sub(r"^(and|or|with)\s+", "", text, flags=re.IGNORECASE)
    words = text.split()
    kept: list[str] = []
    for word in words:
        cleaned = re.sub(r"[^a-z0-9]", "", word.casefold())
        if cleaned in BANNED_SINGLE_WORDS:
            continue
        kept.append(word)
    text = " ".join(kept).strip(" ,;:-")
    if not text:
        return ""
    if text.casefold() in {"between", "categories", "multiple categories"}:
        return ""
    if len(text) > limit:
        text = _truncate(text, limit)
    if find_spam_violations(text):
        return ""
    return text


def _unique_fragments(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    fragments: list[str] = []
    for value in values:
        fragment = _sanitize_fragment_for_copy(value)
        key = fragment.casefold()
        if not fragment or key in seen:
            continue
        seen.add(key)
        fragments.append(fragment)
    return fragments


def _signal_candidates(row: dict[str, str]) -> list[str]:
    raw_signals = _clean_text(row.get("company_signals"))
    values: list[str] = []
    if raw_signals:
        for part in raw_signals.split("|"):
            values.extend(piece.strip() for piece in re.split(r"[.;]", part))
    description = _clean_text(row.get("company_description"))
    if description:
        values.extend(re.split(r"[.;]", description))
    recent_posts = _clean_text(row.get("company_recent_post_summary"))
    if recent_posts:
        for part in recent_posts.split("||"):
            values.extend(piece.strip() for piece in re.split(r"[.;]", part))
    fragments = _unique_fragments(values)
    return [fragment for fragment in fragments if not _contains_any(fragment.casefold(), SKIP_SIGNAL_PHRASES)]


def _pain_candidates(row: dict[str, str]) -> list[str]:
    raw_pain = _clean_text(row.get("company_painpoint"))
    values: list[str] = []
    if raw_pain:
        values.extend(re.split(r"[.;]", raw_pain))
        values.extend(re.split(r",\s+", raw_pain))
    return _unique_fragments(values)


def _offer_candidates(row: dict[str, str]) -> list[str]:
    raw_offer = _clean_text(row.get("company_offer"))
    raw_description = _clean_text(row.get("company_description"))
    values: list[str] = []
    if raw_offer:
        values.extend(re.split(r"[.;]", raw_offer))
        values.extend(re.split(r",\s+", raw_offer))
    if raw_description:
        values.extend(re.split(r"[.;]", raw_description))
    return _unique_fragments(values)


def _fragment_priority(fragment: str, *, bucket: str) -> int:
    lowered = fragment.casefold()
    score = 0
    for hint in ROLE_PRIORITY_HINTS.get(bucket, ROLE_PRIORITY_HINTS["executive_owner"]):
        if hint in lowered:
            score += 3
    if re.search(r"\b\d+\b", fragment):
        score += 1
    if 18 <= len(fragment) <= 100:
        score += 2
    if any(token in lowered for token in ("location", "site", "portal", "ehr", "340b", "audit", "review")):
        score += 1
    return score


def _best_fragment(candidates: list[str], *, bucket: str) -> str:
    if not candidates:
        return ""
    ranked = sorted(
        candidates,
        key=lambda fragment: (_fragment_priority(fragment, bucket=bucket), len(fragment)),
        reverse=True,
    )
    return ranked[0]


def _offer_summary(row: dict[str, str], *, bucket: str, campaign_profile: str) -> str:
    text = _context_text(row)
    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        text = " ".join(
            filter(
                None,
                [
                    _clean_text(row.get("company_description")),
                    _clean_text(row.get("company_offer")),
                    _clean_text(row.get("company_icp")),
                ],
            )
        ).casefold()
        service_parts: list[str] = []
        for needle, label in MSP_SERVICE_TERMS:
            if needle in text and label not in service_parts:
                service_parts.append(label)
        service_parts = service_parts[:4]
        env_suffix = ""
        if _contains_any(text, ("multi client", "multi-client", "client environments", "multiple environments", "tenant")):
            env_suffix = " across multiple client environments"
        elif _contains_any(text, ("distributed", "global", "regional", "remote workforce")):
            env_suffix = " across distributed environments"
        if service_parts:
            if len(service_parts) == 1:
                return service_parts[0] + env_suffix
            if len(service_parts) == 2:
                return f"{service_parts[0]} and {service_parts[1]}{env_suffix}"
            return f"{', '.join(service_parts[:-1])}, and {service_parts[-1]}{env_suffix}"
        candidate = _best_fragment(_offer_candidates(row), bucket=bucket)
        if candidate:
            return candidate
        return "managed IT, security, and client support across multiple environments"

    service_parts: list[str] = []
    for needle, label in SERVICE_TERMS:
        if needle in text and label not in service_parts:
            service_parts.append(label)
    service_parts = service_parts[:4]
    site_suffix = ""
    if _contains_any(text, ("multi-site", "multi site", "locations", "location", "clinic sites")):
        site_suffix = " across multiple sites"
    elif _contains_any(text, ("county", "regional", "service area")):
        site_suffix = " across a distributed clinic footprint"
    if service_parts:
        if len(service_parts) == 1:
            return service_parts[0] + site_suffix
        if len(service_parts) == 2:
            return f"{service_parts[0]} and {service_parts[1]}{site_suffix}"
        return f"{', '.join(service_parts[:-1])}, and {service_parts[-1]}{site_suffix}"
    candidate = _best_fragment(_offer_candidates(row), bucket=bucket)
    if candidate:
        return candidate
    if _contains_any(text, ("340b", "pharmacy")):
        return "clinic and 340b record flow across several teams"
    return "multiple care workflows moving through one clinic network"


def _painpoint_summary(row: dict[str, str], *, bucket: str, campaign_profile: str) -> str:
    text = _context_text(row)
    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        pain_parts: list[str] = []
        if _contains_any(text, ("alert", "noise", "false positive", "triage")):
            pain_parts.append("alert noise and triage drag")
        if _contains_any(text, ("multi client", "multi-client", "client environments", "multiple environments", "tenant")):
            pain_parts.append("multi-client visibility gaps")
        if _contains_any(text, ("audit", "compliance", "evidence", "review")):
            pain_parts.append("evidence and compliance proof")
        if _contains_any(text, ("onboarding", "migration", "transition", "project")):
            pain_parts.append("client onboarding drift")
        if _contains_any(text, ("ticket", "escalation", "help desk", "support queue")):
            pain_parts.append("ticket escalation load")
        if _contains_any(text, ("ransomware", "phishing", "incident", "threat", "mdr", "xdr", "soc")):
            pain_parts.append("threat response pressure")
        if _contains_any(text, ("tool sprawl", "siem", "stack", "multiple tools")):
            pain_parts.append("tool sprawl")
        if pain_parts:
            return " and ".join(pain_parts[:2])
        candidate = _best_fragment(_pain_candidates(row), bucket=bucket)
        if candidate:
            return candidate
        if bucket == "it_ops":
            return "keeping visibility across shared tools, client environments, and change-heavy service work"
        if bucket == "security":
            return "separating real risk from alert noise and keeping defensible evidence"
        if bucket == "commercial":
            return "protecting client trust while delivery teams carry more moving parts"
        if bucket == "admin":
            return "keeping evidence chase and coordination work from piling up"
        return "scaling service delivery without quiet risk piling up across clients"

    pain_parts: list[str] = []
    if _contains_any(text, ("multi-site", "multi site", "distributed", "across locations", "clinic locations")):
        pain_parts.append("multi-site record oversight")
    if _contains_any(text, ("ephi", "integrity", "file integrity", "record integrity")):
        pain_parts.append("ePHI integrity visibility")
    if _contains_any(text, ("limited it", "lean it", "limited resources", "resource constraints")):
        pain_parts.append("lean team coverage")
    if _contains_any(text, ("audit", "ocr", "review", "compliance")):
        pain_parts.append("audit review pressure")
    if pain_parts:
        return " and ".join(pain_parts[:2])
    candidate = _best_fragment(_pain_candidates(row), bucket=bucket)
    if candidate:
        return candidate
    if bucket == "technical_it":
        return "keeping trail visibility across shared paths and exports"
    if bucket == "finance":
        return "audit prep drag and late-stage review work"
    if bucket == "operations":
        return "cross-site cleanup when a record path drifts"
    if bucket == "security_compliance_risk":
        return "showing a clean trail during review"
    return "quiet record drift surfacing too late"


def _signal_summary(row: dict[str, str], *, bucket: str, campaign_profile: str) -> str:
    candidate = _best_fragment(_signal_candidates(row), bucket=bucket)
    if candidate:
        return candidate
    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        if bucket == "it_ops":
            return "managed service work, shared tooling, and client environments all in the mix"
        if bucket == "security":
            return "security operations and evidence demands moving at the same time"
        if bucket == "commercial":
            return "client-facing growth and delivery load moving together"
        if bucket == "admin":
            return "coordination and follow-up load sitting close to the client record trail"
        return "multiple client environments and delivery pressure moving at once"
    if bucket == "technical_it":
        return "shared record paths and patient-facing systems in the mix"
    if bucket == "finance":
        return "growth and review load moving at the same time"
    if bucket == "operations":
        return "multi-site workflow strain across the clinic footprint"
    return "a lot moving across the clinic footprint"


def _msp_signal_fragments(row: dict[str, str]) -> list[str]:
    fragments: list[str] = []
    raw_signals = _clean_text(row.get("company_signals"))
    if raw_signals:
        fragments.extend(_clean_text(part) for part in raw_signals.split("|") if _clean_text(part))

    recent_posts = _clean_text(row.get("company_recent_post_summary"))
    if recent_posts:
        for part in recent_posts.split("||"):
            cleaned = _sanitize_fragment_for_copy(part, limit=180)
            lowered = cleaned.casefold()
            if not cleaned:
                continue
            if _contains_weak_signal_pattern(lowered):
                continue
            if any(
                trigger in lowered
                for trigger in (
                    "hiring",
                    "acquisition",
                    "acquired",
                    "expansion",
                    "launch",
                    "breach",
                    "security",
                    "cloud",
                    "microsoft",
                    "ai",
                    "assessment",
                    "compliance",
                )
            ):
                fragments.append(cleaned)

    seen: set[str] = set()
    approved: list[str] = []
    for fragment in fragments:
        cleaned = _sanitize_fragment_for_copy(fragment, limit=160)
        lowered = cleaned.casefold()
        if not cleaned or lowered in seen:
            continue
        seen.add(lowered)
        if any(pattern in lowered for pattern in MSP_MSSP_NEGATIVE_HINTS):
            continue
        approved.append(cleaned)
    return approved


def _msp_signal_priority(signal: str) -> int:
    lowered = signal.casefold()
    score = 0
    if any(
        token in lowered
        for token in (
            "acquisition",
            "acquired",
            "investment",
            "expansion",
            "first us hire",
            "launch",
            "applied for",
            "growth strategy",
            "revenue by",
            "pe-backed",
            "private equity",
        )
    ):
        score += 5
    if any(
        token in lowered
        for token in (
            "security assessment",
            "breach prevention",
            "vulnerability assessment",
            "penetration testing",
            "pen testing",
            "aaa rating",
            "email and cloud security",
            "incident reporting",
        )
    ):
        score += 6
    if any(token in lowered for token in ("breach prevention", "copilot", "tool consolidation", "replaced 6 tools", "saved $", "saved £")):
        score += 3
    if any(token in lowered for token in ("replaced 6 tools", "saved $", "saved £")):
        score += 2
    if any(
        token in lowered
        for token in (
            "replaced 6 tools",
            "tool consolidation",
            "partner program",
            "saved $",
            "saved £",
            "msp guide",
            "safeguard your clients",
        )
    ):
        score += 6
    if any(
        token in lowered
        for token in (
            "hiring",
            "cloud/ai",
            "cloud",
            "microsoft",
            "ai-powered",
            "ai powered",
            "copilot",
            "ai security",
            "c-suite",
            "board members",
            "regulated industries",
        )
    ):
        score += 5
    if any(token in lowered for token in ("client base", "mid-market", "regional", "canadian", "multi-country")):
        score += 3
    if len(signal) > 100 or "..." in signal:
        score -= 2
    if "employees indicating" in lowered or "followers" in lowered:
        score -= 4
    if any(
        token in lowered
        for token in (
            "valuation",
            "total funding",
            "arr in",
            "customer acquisition",
            "vp of marketing",
            "joined as cfo",
            "women of the channel",
            "fast 500",
            "community engagement",
        )
    ):
        score -= 6
    if _contains_weak_signal_pattern(lowered):
        score -= 5
    if any(token in lowered for token in ("recognition", "tech elite", "high performer")):
        score -= 4
    if re.search(r"\b\d+\b", signal):
        score += 1
    return score


def _ranked_msp_signals(row: dict[str, str]) -> list[str]:
    signals = _msp_signal_fragments(row)
    return sorted(signals, key=lambda value: (-_msp_signal_priority(value), len(value)))


def _best_msp_signal(row: dict[str, str]) -> str:
    ranked = _ranked_msp_signals(row)
    if not ranked:
        return ""
    return ranked[0]


def _secondary_msp_signal(row: dict[str, str]) -> str:
    ranked = _ranked_msp_signals(row)
    if len(ranked) < 2:
        return ""
    return ranked[1]


def _humanized_msp_signal_candidates(*, row: dict[str, str], company_name: str) -> list[tuple[str, str]]:
    approved: list[tuple[str, str]] = []
    for signal in _ranked_msp_signals(row):
        line = _humanize_msp_signal(signal, company_name=company_name)
        if not line:
            continue
        approved.append((signal, line))
    return approved


def _msp_client_phrase(row: dict[str, str]) -> str:
    icp = _clean_text(row.get("company_icp")).casefold()
    description = _clean_text(row.get("company_description")).casefold()
    text = " ".join(filter(None, [icp, description]))
    if not text:
        return ""
    if "canadian mid-market" in text:
        return "Canadian mid-market clients"
    if "mid-market" in text:
        return "mid-market clients"
    if "enterprise" in text:
        return "enterprise clients"
    if "public sector" in text:
        return "public sector clients"
    matches: list[str] = []
    for snippet in INDUSTRY_SNIPPETS:
        if snippet in text and snippet not in matches and snippet not in {"mid-market", "enterprise", "canadian mid-market"}:
            matches.append(snippet)
    if matches:
        return "multi-vertical clients"
    return ""


def _humanize_msp_signal(signal: str, *, company_name: str) -> str:
    cleaned = _sanitize_fragment_for_copy(signal, limit=140)
    lowered = cleaned.casefold()
    if not cleaned:
        return ""
    if _contains_weak_signal_pattern(lowered):
        return ""
    if any(
        token in lowered
        for token in ("valuation", "total funding", "customer acquisition", "vp of marketing", "joined as cfo", "women of the channel")
    ):
        return ""
    if "buying software is easy" in lowered:
        return ""
    if "regional" in lowered and "presence" in lowered:
        return ""
    if "investment from" in lowered:
        return f"Saw {company_name} recently took PE backing."
    if "recent acquisition by" in lowered:
        return f"Saw {company_name} just moved through an acquisition."
    if "acquisition of" in lowered:
        target = cleaned.split("of", 1)[1].strip(" .") if "of" in cleaned else "a new team"
        return f"Saw {company_name} recently added {target}."
    if "first us hire" in lowered or "us market expansion" in lowered:
        return f"Saw {company_name} is opening up in the US."
    if "hiring" in lowered:
        return f"Saw {company_name} is actively hiring."
    if "security assessment" in lowered and "breach prevention" in lowered:
        return f"Saw {company_name} has been pushing breach-prevention assessment work."
    if "published microsoft 365 copilot" in lowered or "copilot adoption guidance" in lowered:
        return f"Saw {company_name} has been publishing Microsoft 365 Copilot rollout guidance."
    if "3-part ai security analysis" in lowered or "c-suite focused ai security content" in lowered:
        return f"Saw {company_name} has been publishing board-level AI security guidance."
    if "targeting £100m revenue" in lowered or "targeting $100m revenue" in lowered:
        return f"Saw {company_name} is pushing toward 100M in revenue."
    if "penetration testing" in lowered or "vulnerability assessment expertise" in lowered:
        return f"Saw {company_name} is leaning into pen testing and vulnerability work."
    if "ai-powered security positioning" in lowered or "ai powered security positioning" in lowered:
        return f"Saw {company_name} is leaning into AI-powered security work."
    if "se labs aaa rating" in lowered or "aaa rating" in lowered:
        return f"Saw {company_name} just picked up SE Labs AAA ratings for email and cloud security."
    if any(token in lowered for token in ("replaced 6 tools with 1 platform", "replacing 6 tools", "tool consolidation", "saved $400k", "saved $400,000")):
        return f"Saw {company_name} is leaning hard into security tool consolidation."
    if "partner program for msps" in lowered or "partner program" in lowered:
        return f"Saw {company_name} is leaning into MSP partner growth."
    if "high performer" in lowered and any(token in lowered for token in ("vulnerability", "cloud", "infrastructure", "security")):
        return f"Saw {company_name} just picked up G2 recognition across cloud, infra, and vulnerability work."
    if "cloud/ai" in lowered or "microsoft cloud" in lowered:
        return f"Saw {company_name} is leaning harder into cloud and AI work."
    if "multi-country presence" in lowered:
        return f"Saw {company_name} is operating across multiple regions."
    if "client base includes" in lowered:
        return f"Saw {company_name} already supports a pretty broad client mix."
    if "acquisitions in recent years" in lowered:
        return f"Saw {company_name} has been integrating recent acquisitions."
    if "growth trajectory indicates need for sustained customer acquisition" in lowered:
        return ""
    if "regulated industries" in lowered:
        return f"Saw {company_name} is leaning into regulated-industry buyers."
    if "legal sector" in lowered or "solicitors conference" in lowered:
        return f"Saw {company_name} has been leaning into legal-sector clients."
    if "active in canadian mid-market" in lowered:
        return f"Saw {company_name} is leaning into the Canadian mid-market."
    if "employees indicating" in lowered:
        return ""
    return ""


def _msp_signal_category(signal: str) -> str:
    lowered = signal.casefold()
    if any(
        token in lowered
        for token in (
            "acquisition",
            "acquired",
            "investment",
            "growth strategy",
            "revenue by",
            "first us hire",
            "expansion",
            "private equity",
            "pe-backed",
        )
    ):
        return "growth"
    if any(
        token in lowered
        for token in (
            "copilot",
            "microsoft 365",
            "ai security",
            "ai-powered",
            "ai powered",
            "cloud/ai",
            "board-level",
            "board members",
            "c-suite",
        )
    ):
        return "ai_security"
    if any(
        token in lowered
        for token in (
            "vulnerability",
            "penetration testing",
            "pen testing",
            "security assessment",
            "breach prevention",
            "aaa rating",
            "email and cloud security",
            "incident reporting",
        )
    ):
        return "security_delivery"
    if any(
        token in lowered
        for token in (
            "replaced 6 tools",
            "tool consolidation",
            "partner program",
            "saved $",
            "saved £",
            "msp guide",
            "safeguard your clients",
        )
    ):
        return "consolidation"
    if any(token in lowered for token in ("regulated industries", "legal sector", "solicitors conference", "healthcare", "finance")):
        return "vertical_focus"
    if any(token in lowered for token in ("high performer", "recognition", "fast 500", "women of the channel", "aaa rating")):
        return "market_validation"
    return "general"


def _msp_signal_impact_line(*, bucket: str, signal: str) -> str:
    category = _msp_signal_category(signal)
    if bucket == "it_ops":
        mapping = {
            "growth": "Usually that means more handoffs, inherited tooling, and change paths to keep visible.",
            "ai_security": "Usually that means more rollout files, shared tools, and client-side changes moving at once.",
            "security_delivery": "Usually that means more assessment outputs, remediation handoffs, and evidence paths to keep visible.",
            "consolidation": "Usually that means more policy changes, admin outputs, and migration paths to keep straight.",
            "vertical_focus": "Usually that means tighter client requirements and more service paths to keep visible.",
            "market_validation": "Usually that means more client environments and more shared tools to keep straight.",
            "general": "Usually that means more shared tools, path changes, and handoffs to watch.",
        }
        return mapping[category]
    if bucket == "security":
        mapping = {
            "growth": "Usually that means more inherited controls, more evidence surfaces, and more noise to defend.",
            "ai_security": "Usually that means more AI-related policy, enablement, and evidence paths to keep defensible.",
            "security_delivery": "Usually that means more assessment outputs, alert context, and review-heavy trails to keep clean.",
            "consolidation": "Usually that means more control changes and more review paths tied to tool consolidation.",
            "vertical_focus": "Usually that means tighter client expectations and more evidence-heavy review paths.",
            "market_validation": "Usually that means more client scrutiny on proof and trail quality.",
            "general": "Usually that means more evidence-heavy paths and more noise to sort through.",
        }
        return mapping[category]
    if bucket == "commercial":
        mapping = {
            "growth": "Usually that means more client expectations and more delivery questions the commercial team ends up absorbing.",
            "ai_security": "Usually that means more strategic security conversations and more follow-up if delivery gets fuzzy.",
            "security_delivery": "Usually that means more client questions around proof, remediation, and what changed.",
            "consolidation": "Usually that means more pressure to explain operational simplicity without surprises.",
            "vertical_focus": "Usually that means less room for noisy follow-up with clients in tighter sectors.",
            "market_validation": "Usually that means buyers expect the delivery side to stay just as sharp.",
            "general": "Usually that means client teams need cleaner answers when something shifts.",
        }
        return mapping[category]
    if bucket == "admin":
        mapping = {
            "growth": "Usually that means more coordination, more inherited docs, and more review chase work.",
            "ai_security": "Usually that means more rollout notes, more follow-up, and more handoff-heavy documentation.",
            "security_delivery": "Usually that means more evidence chase, more report handling, and more review drag.",
            "consolidation": "Usually that means more policy updates and more follow-up across shared docs.",
            "vertical_focus": "Usually that means tighter review expectations and more documentation drag.",
            "market_validation": "Usually that means more follow-up and more admin cleanup behind the scenes.",
            "general": "Usually that means more follow-up and review chase work.",
        }
        return mapping[category]
    mapping = {
        "growth": "Usually that means more teams, more tooling, and more delivery paths that can drift quietly.",
        "ai_security": "Usually that means more rollout work and more sensitive client-side handoffs to keep straight.",
        "security_delivery": "Usually that means more evidence-heavy client work and more paths leadership only hears about late.",
        "consolidation": "Usually that means more client environments and more change paths to keep clean.",
        "vertical_focus": "Usually that means tighter client expectations and less room for noisy follow-up.",
        "market_validation": "Usually that means buyers expect the delivery side to stay sharp as the footprint grows.",
        "general": "Usually that means more shared tools, handoffs, and client-side drift to keep straight.",
    }
    return mapping[category]


def _msp_signal_context_line(*, bucket: str, company_name: str, signal: str) -> str:
    humanized = _humanize_msp_signal(signal, company_name=company_name)
    if not humanized:
        return ""
    return f"{humanized} {_msp_signal_impact_line(bucket=bucket, signal=signal)}"


def _msp_followup_fallback_line(*, bucket: str, signal: str) -> str:
    category = _msp_signal_category(signal)
    if bucket == "it_ops":
        mapping = {
            "growth": "Usually the drag shows up in inherited tooling, shared reports, and cross-team handoffs.",
            "ai_security": "Usually the drag shows up in rollout docs, shared tools, and client-side handoffs.",
            "security_delivery": "Usually the drag shows up in assessment reports, remediation files, and what-changed follow-up.",
            "consolidation": "Usually the drag shows up in policy outputs, admin exports, and migration handoffs.",
            "vertical_focus": "Usually the drag shows up in tighter client review paths and escalation handoffs.",
            "market_validation": "Usually the drag shows up once more client environments land on the same toolset.",
            "general": "Usually the drag shows up in shared tools, reporting outputs, and handoff-heavy paths.",
        }
        return mapping[category]
    if bucket == "security":
        mapping = {
            "growth": "Usually the drag shows up in inherited controls, escalation files, and review-heavy evidence paths.",
            "ai_security": "Usually the drag shows up in policy outputs, enablement files, and review-heavy trails.",
            "security_delivery": "Usually the drag shows up in assessment outputs, remediation proof, and evidence chase.",
            "consolidation": "Usually the drag shows up in control changes, admin exports, and audit-heavy handoffs.",
            "vertical_focus": "Usually the drag shows up when tighter client reviews hit the same shared trail.",
            "market_validation": "Usually the drag shows up once client scrutiny shifts from promise to proof.",
            "general": "Usually the drag shows up in evidence-heavy outputs and what-changed follow-up.",
        }
        return mapping[category]
    if bucket == "commercial":
        mapping = {
            "growth": "Usually the drag shows up when delivery questions spill over into renewal or account conversations.",
            "ai_security": "Usually the drag shows up when rollout friction becomes a client-facing conversation.",
            "security_delivery": "Usually the drag shows up when clients ask for cleaner proof on what changed.",
            "consolidation": "Usually the drag shows up when buyers expect simpler delivery than the back-end reality.",
            "vertical_focus": "Usually the drag shows up when tighter-sector clients ask hard questions fast.",
            "market_validation": "Usually the drag shows up when stronger market positioning raises delivery expectations.",
            "general": "Usually the drag shows up when client-facing teams inherit the awkward follow-up.",
        }
        return mapping[category]
    if bucket == "admin":
        mapping = {
            "growth": "Usually the drag shows up in inherited docs, more follow-up, and coordination-heavy handoffs.",
            "ai_security": "Usually the drag shows up in rollout notes, cross-team follow-up, and shared documentation.",
            "security_delivery": "Usually the drag shows up in remediation files, review packets, and evidence chase.",
            "consolidation": "Usually the drag shows up in policy updates, exports, and back-and-forth cleanup.",
            "vertical_focus": "Usually the drag shows up in tighter client review packets and more documentation chase.",
            "market_validation": "Usually the drag shows up once more client work lands on the same coordination paths.",
            "general": "Usually the drag shows up in follow-up, review files, and handoff-heavy folders.",
        }
        return mapping[category]
    mapping = {
        "growth": "Usually the drag shows up in inherited files, reporting outputs, and cross-team handoffs.",
        "ai_security": "Usually the drag shows up in rollout docs, client-side changes, and shared handoffs.",
        "security_delivery": "Usually the drag shows up in assessment reports, remediation files, and proof-heavy follow-up.",
        "consolidation": "Usually the drag shows up in policy changes, admin outputs, and tool-migration handoffs.",
        "vertical_focus": "Usually the drag shows up when tighter-sector clients want cleaner answers faster.",
        "market_validation": "Usually the drag shows up once stronger positioning raises client delivery expectations.",
        "general": "Usually the drag shows up in shared files, client reports, and handoff-heavy paths.",
    }
    return mapping[category]


def _msp_followup_line(*, bucket: str, company_name: str, row: dict[str, str]) -> str:
    approved = _humanized_msp_signal_candidates(row=row, company_name=company_name)
    if len(approved) >= 2:
        return approved[1][1]
    primary_signal = approved[0][0] if approved else _best_msp_signal(row)
    if primary_signal:
        return _msp_followup_fallback_line(bucket=bucket, signal=primary_signal)
    return _msp_motion_line(bucket=bucket, company_name=company_name, row=row)


def _msp_scope_line(*, bucket: str, company_name: str, row: dict[str, str], signal: str = "") -> str:
    category = _msp_signal_category(signal or _best_msp_signal(row))
    if category == "growth":
        if bucket == "security":
            return f"First place I'd watch at {company_name} is inherited controls, escalation files, and evidence-heavy handoffs from newly added teams."
        return f"First place I'd watch at {company_name} is inherited delivery files, reporting outputs, and handoff paths from newly added teams."
    if category == "ai_security":
        if bucket == "security":
            return f"First place I'd watch at {company_name} is AI rollout docs, policy outputs, and security handoff files."
        return f"First place I'd watch at {company_name} is rollout docs, enablement files, and client-side handoff paths."
    if category == "security_delivery":
        if bucket == "commercial":
            return f"First place I'd watch at {company_name} is the assessment reports and remediation handoffs clients ask about under pressure."
        return f"First place I'd watch at {company_name} is assessment outputs, client reports, and remediation handoff paths."
    if category == "consolidation":
        return f"First place I'd watch at {company_name} is policy outputs, admin exports, and tool-migration handoff files."
    if category == "vertical_focus":
        if bucket == "security":
            return f"First place I'd watch at {company_name} is regulated-client evidence packets, review files, and escalation paths."
        return f"First place I'd watch at {company_name} is client reports, review files, and handoff-heavy paths for regulated accounts."
    if bucket == "it_ops":
        return f"First place I'd watch at {company_name} is tooling outputs, client reports, and handoff files."
    if bucket == "security":
        return f"First place I'd watch at {company_name} is evidence-heavy outputs, client reports, and escalation files."
    if bucket == "commercial":
        return f"Usually this shows up first in the delivery paths customers ask about under pressure."
    if bucket == "admin":
        return f"Usually this shows up first in shared docs, review files, and handoff-heavy folders."
    return f"First place I'd watch at {company_name} is shared delivery files, reporting outputs, and handoff paths."


def _msp_motion_line(*, bucket: str, company_name: str, row: dict[str, str]) -> str:
    offer = _offer_summary(row, bucket=bucket, campaign_profile=CAMPAIGN_PROFILE_MSP_MSSP)
    offer_phrase = _compact_msp_offer_phrase(offer, bucket=bucket)
    client_phrase = _msp_client_phrase(row)
    if client_phrase:
        return f"Looks like {company_name} is carrying {offer_phrase} for {client_phrase.lower()}."
    if bucket in {"it_ops", "security"}:
        return f"Looks like {company_name} is carrying {offer_phrase} across client environments."
    return f"Looks like {company_name} is carrying {offer_phrase}."


def _signal_line_intro(company_name: str, signal: str) -> str:
    lowered = signal.casefold()
    starter_words = (
        "operating",
        "uses",
        "using",
        "active",
        "recent",
        "serves",
        "hiring",
        "opening",
        "expansion",
        "partner",
        "participates",
        "launch",
    )
    if "indicate" in lowered:
        return f"Saw signals like {signal} at {company_name}"
    if lowered.startswith(starter_words):
        signal_fragment = signal[0].lower() + signal[1:] if signal[:1].isupper() else signal
        return f"Saw {company_name} {signal_fragment}"
    return f"Saw signals like {signal} at {company_name}"


def _humanized_signal(signal: str, *, company_name: str, bucket: str) -> str:
    lowered = signal.casefold()
    if bucket in {"leadership", "it_ops", "security", "commercial", "admin"}:
        if lowered.startswith(("recent", "hiring", "partner", "launch", "opened", "expanding", "growing")):
            return f"Saw {company_name} {signal[0].lower() + signal[1:] if signal[:1].isupper() else signal}"
        if any(term in lowered for term in ("client", "managed", "security", "soc", "mdr", "siem", "cloud", "compliance", "backup")):
            return f"Saw signals like {signal} at {company_name}"
        return _signal_line_intro(company_name, signal)
    if "patient portal" in lowered:
        portal_name = re.sub(r"^(uses|using)\s+", "", signal, flags=re.IGNORECASE).strip()
        return f"Saw {company_name} uses {portal_name}"
    if "eclinicalworks" in lowered or "cloud ehr" in lowered:
        return f"Saw {company_name} is running {signal}"
    if "operating " in lowered or "location" in lowered or "mobile unit" in lowered:
        return f"Saw {company_name} {signal[0].lower() + signal[1:] if signal[:1].isupper() else signal}"
    if "multiple service lines" in lowered or "diverse service lines" in lowered:
        return f"Saw {company_name} is running {signal.lower()}"
    if bucket == "operations":
        return f"Saw {company_name} is carrying {signal.lower()}"
    return _signal_line_intro(company_name, signal)


def _offer_line(row: dict[str, str], *, bucket: str, campaign_profile: str) -> str:
    offer = _offer_summary(row, bucket=bucket, campaign_profile=campaign_profile)
    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        if bucket == "it_ops":
            return f"Running {offer} usually means more client-side change paths, shared tools, and service handoffs to keep visible."
        if bucket == "security":
            return f"Running {offer} usually leaves more evidence surfaces and alert paths that need a defensible trail."
        if bucket == "commercial":
            return f"Running {offer} usually ties client trust more closely to how quickly delivery teams can explain what changed."
        if bucket == "admin":
            return f"Running {offer} usually creates more follow-up, coordination, and review work when the trail is thin."
        return f"Running {offer} usually raises the odds of quiet client risk showing up late."
    if bucket == "technical_it":
        return f"Running {offer} usually means more record paths, exports, and shared folders to keep clean."
    if bucket == "finance":
        return f"Running {offer} tends to widen the record trail leadership ends up answering for."
    if bucket == "operations":
        return f"Running {offer} usually creates more handoffs and more report paths to keep straight."
    if bucket == "security_compliance_risk":
        return f"Running {offer} usually leaves more record surfaces that need a clean review trail."
    return f"Running {offer} usually leaves more record paths that leadership only hears about when something drifts."


def _compact_msp_offer_phrase(offer: str, *, bucket: str | None = None) -> str:
    text = _clean_text(offer)
    if not text:
        return "managed IT, cloud, and security work"
    text = re.sub(r"\s+across .*?$", "", text, flags=re.IGNORECASE).strip(" ,.")
    lowered = text.casefold()
    replacements = (
        ("it infrastructure consulting", "infra"),
        ("infrastructure consulting", "infra"),
        ("managed security", "security"),
        ("cybersecurity services", "security"),
        ("managed IT", "managed IT"),
        ("IT support", "IT support"),
        ("cloud services", "cloud"),
        ("cloud migration services", "cloud migration"),
        ("network operations", "infra"),
        ("backup and disaster recovery", "backup / DR"),
        ("compliance services", "compliance"),
        ("IT consulting", "infra"),
        ("SOC services", "SOC"),
        ("SIEM coverage", "SIEM"),
        ("vulnerability assessment services", "vulnerability assessment"),
    )
    for source, target in replacements:
        lowered = lowered.replace(source.casefold(), target)
    lowered = lowered.replace(", and ", ", ")
    parts = re.split(r",\s*|\s+and\s+", lowered)
    compact_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = re.sub(r"^and\s+", "", part.strip(" ,."))
        if not cleaned or cleaned in {"multiple client environments", "distributed environments"}:
            continue
        if cleaned == "cloud" and any(existing == "cloud migration" for existing in compact_parts):
            continue
        if cleaned == "security" and any(existing in {"managed security", "vulnerability assessment"} for existing in compact_parts):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        compact_parts.append(cleaned)
    compact_parts = compact_parts[:3]
    if not compact_parts:
        return "managed IT, cloud, and security work"
    if len(compact_parts) == 1:
        joined = compact_parts[0]
    elif len(compact_parts) == 2:
        joined = f"{compact_parts[0]} and {compact_parts[1]}"
    else:
        joined = f"{compact_parts[0]}, {compact_parts[1]}, and {compact_parts[2]}"
    if bucket in {"leadership", "commercial", "admin"}:
        joined = joined.replace("cloud migration", "cloud")
        joined = joined.replace("vulnerability assessment", "vulnerability")
    elif bucket in {"it_ops", "security"}:
        joined = joined.replace("vulnerability assessment", "vulnerability work")
    if not any(
        token in joined
        for token in ("work", "support", "services", "coverage", "operations", "MDR", "XDR", "SOC", "SIEM", "vCIO", "vCISO")
    ):
        joined = f"{joined} work"
    return joined


def _msp_opening_context_line(*, company_name: str, offer: str, bucket: str) -> str:
    return f"{company_name} is deep in {_compact_msp_offer_phrase(offer, bucket=bucket)}."


def _copy_company_name(normalized: CompanyNameNormalization, raw_name: str) -> str:
    base_name = normalized.short_name or normalized.normalized_name or raw_name
    safe_name = spam_safe_company_name(base_name)
    if (
        base_name
        and safe_name
        and safe_name != base_name
        and (
            "&" in base_name
            or "." in base_name
            or re.search(r"\b[A-Z]{2,}\b", base_name) is not None
        )
    ):
        return _clean_text(base_name)
    if safe_name:
        return safe_name
    fallback = normalized.short_name or normalized.normalized_name or raw_name
    return _clean_text(fallback) or "the team"


ROLE_PERSONALIZATION_BRIEFS = {
    "technical_it": "Write for a technical buyer. Prefer patient portal, EHR, exports, shared folders, trail visibility, agentless coverage, webhook or email signal.",
    "finance": "Write for a finance leader. Prefer OCR review load, audit prep drag, cleanup cost, budget pressure, late-stage review work.",
    "operations": "Write for an operations leader. Prefer rerun reports, cross-site cleanup, handoffs, clinic-by-clinic chase, workflow drag.",
    "executive_owner": "Write for an executive. Prefer leadership late visibility, patient trust, board-level review, oversight time, mission continuity.",
    "security_compliance_risk": "Write for a compliance or risk leader. Prefer review packet quality, evidence trail, control trail, defensible review posture.",
    "leadership": "Write for an MSP or MSSP leader. Prefer client trust, delivery scale, visibility across client environments, quiet operational risk, and leadership time.",
    "it_ops": "Write for an MSP or MSSP IT ops owner. Prefer change visibility, client environments, shared tools, ticket escalations, and agentless coverage.",
    "security": "Write for an MSP or MSSP security owner. Prefer alert noise, evidence quality, review readiness, client security operations, and defensible trails.",
    "commercial": "Write for a commercial owner. Prefer client trust, renewals, proof of delivery quality, and reducing surprises the account team has to explain.",
    "admin": "Write for an admin or coordinator. Prefer follow-up drag, evidence chase, documentation load, and smoother coordination.",
}


class MiniMaxPersonalizationWriter:
    def __init__(self, client: MiniMaxClient) -> None:
        self._client = client

    async def write_first_line(
        self,
        *,
        company_name: str,
        role_title: str,
        role_description: str,
        bucket: str,
        row: dict[str, str],
        campaign_profile: str,
    ) -> MiniMaxPersonalizationResult:
        bucket_signal = _signal_summary(row, bucket=bucket, campaign_profile=campaign_profile)
        bucket_pain = _painpoint_summary(row, bucket=bucket, campaign_profile=campaign_profile)
        bucket_offer = _offer_summary(row, bucket=bucket, campaign_profile=campaign_profile)
        payload = {
            "company_name": company_name,
            "role_title": role_title,
            "role_description": _truncate(role_description, 220),
            "role_bucket": bucket,
            "campaign_profile": campaign_profile,
            "role_brief": ROLE_PERSONALIZATION_BRIEFS.get(bucket, ROLE_PERSONALIZATION_BRIEFS["executive_owner"]),
            "company_signal": bucket_signal,
            "company_painpoint": bucket_pain,
            "company_offer": bucket_offer,
            "company_offer_compact": _compact_msp_offer_phrase(bucket_offer, bucket=bucket)
            if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP
            else bucket_offer,
            "company_description": _truncate(_clean_text(row.get("company_description")), 220),
        }
        response = await self._client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write one cold-email opener sentence. "
                        "Return JSON only with schema {\"p1\":\"...\"}. "
                        "No code fences. No explanation. No greeting. "
                        "Hard max 14 words. "
                        "Sound plainspoken and human, not corporate. "
                        "Start with the company name or Noticed. "
                        "Prefer concrete service context over raw social-post phrasing. "
                        "Avoid hashtags, hype, awards language, and exclamation marks. "
                        "Make it role-specific. "
                        "Use lighter jargon for leadership, commercial, and admin. "
                        "Use medium technical detail for IT ops. "
                        "Use technical but readable wording for security."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Write one sentence, 8-14 words.\n"
                        "Must include the company name exactly.\n"
                        "Use the role and company context below.\n"
                        "Keep it conversational.\n"
                        "Return JSON only.\n"
                        f"{json.dumps(payload, ensure_ascii=False)}"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=260,
        )
        raw_payload = response.data if isinstance(response.data, dict) else {}
        parsed = _extract_json_object(_message_content(raw_payload)) or {}
        line = _clean_text(parsed.get("p1"))
        return MiniMaxPersonalizationResult(
            p1=line,
            raw_response=raw_payload,
            parsed_response=parsed,
        )


def _personalization_lines(
    *,
    bucket: str,
    company_name: str,
    row: dict[str, str],
    campaign_profile: str,
) -> tuple[str, str, str, str]:
    signal = _signal_summary(row, bucket=bucket, campaign_profile=campaign_profile)
    painpoint = _painpoint_summary(row, bucket=bucket, campaign_profile=campaign_profile)
    offer = _offer_summary(row, bucket=bucket, campaign_profile=campaign_profile)
    intro = _humanized_signal(signal, company_name=company_name, bucket=bucket)

    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        approved_signals = _humanized_msp_signal_candidates(row=row, company_name=company_name)
        primary_signal = approved_signals[0][0] if approved_signals else _best_msp_signal(row)
        signal_line = approved_signals[0][1] if approved_signals else ""
        if signal_line:
            signal_line = f"{signal_line} {_msp_signal_impact_line(bucket=bucket, signal=primary_signal)}"
        else:
            signal_line = _msp_signal_context_line(bucket=bucket, company_name=company_name, signal=primary_signal)
        if not signal_line:
            motion_line = _msp_motion_line(bucket=bucket, company_name=company_name, row=row)
            fallback_impact = _msp_followup_fallback_line(bucket=bucket, signal=primary_signal or "general")
            signal_line = f"{motion_line} {fallback_impact}".strip()
        followup_signal_line = _msp_followup_line(bucket=bucket, company_name=company_name, row=row)
        if bucket == "leadership":
            return (
                signal_line,
                followup_signal_line,
                _msp_scope_line(bucket=bucket, company_name=company_name, row=row, signal=primary_signal),
                "If delivery risk or security ops sits elsewhere, happy to send this there.",
            )
        if bucket == "it_ops":
            return (
                signal_line,
                followup_signal_line,
                _msp_scope_line(bucket=bucket, company_name=company_name, row=row, signal=primary_signal),
                "If this lives with another ops owner, happy to send it there.",
            )
        if bucket == "security":
            return (
                signal_line,
                followup_signal_line,
                _msp_scope_line(bucket=bucket, company_name=company_name, row=row, signal=primary_signal),
                "If this lands with another security owner, happy to send it there.",
            )
        if bucket == "commercial":
            return (
                signal_line,
                followup_signal_line,
                _msp_scope_line(bucket=bucket, company_name=company_name, row=row, signal=primary_signal),
                "If someone else owns delivery quality there, happy to send it over.",
            )
        if bucket == "admin":
            return (
                signal_line,
                followup_signal_line,
                _msp_scope_line(bucket=bucket, company_name=company_name, row=row, signal=primary_signal),
                "If this sits with another coordinator or ops owner, happy to send it there.",
            )

    if bucket == "technical_it":
        return (
            f"{intro}; for IT, that usually means more EHR paths, exports, and shared folders to keep under watch.",
            f"The pain point around {painpoint.lower()} is usually where lean IT teams lose trail visibility.",
            f"Running {offer} usually means more record paths, exports, and shared folders to keep clean.",
            f"Between {signal.lower()} and {painpoint.lower()}, I can see why agentless path coverage would matter here.",
        )

    if bucket == "finance":
        return (
            f"{intro}; that usually raises OCR review load and the cost of late-stage cleanup.",
            f"The pain point around {painpoint.lower()} usually shows up as audit prep drag and unplanned review time.",
            f"Running {offer} tends to widen the record trail leadership ends up answering for.",
            f"With {signal.lower()} in motion, it makes sense to stay ahead of record drift before it turns into budget pressure.",
        )

    if bucket == "operations":
        return (
            f"Saw {company_name} is running {offer}; for ops, that usually means more rerun reports, more handoffs, and more cross-site cleanup when a record path drifts.",
            f"The pain point around {painpoint.lower()} usually turns into staff time spent tracing what changed where.",
            f"With {signal.lower()} already in the mix, a small path issue can turn into a clinic-by-clinic chase fast.",
            f"When {offer} is already moving across sites, quiet record drift usually turns into avoidable cleanup for the ops team.",
        )

    if bucket == "security_compliance_risk":
        return (
            f"{intro}; that usually leaves more review pressure on the trail behind each file change.",
            f"The pain point around {painpoint.lower()} is usually where review packets start getting harder to defend.",
            _offer_line(row, bucket=bucket, campaign_profile=campaign_profile),
            f"With {signal.lower()} already in play, a thin trail can become a review issue faster than teams expect.",
        )

    return (
        f"{intro}; that usually raises the odds of quiet record drift landing on leadership late.",
        f"The pain point around {painpoint.lower()} is usually where patient trust and oversight time start getting pulled in.",
        f"Running {offer} usually leaves more record paths that leadership only hears about when something drifts.",
        f"Between {signal.lower()} and {painpoint.lower()}, I can see why trail visibility would matter at this stage.",
    )


def _validated_copy_set(copy_set: RoleBucketCopySet) -> RoleBucketCopySet:
    for index, subject in enumerate((copy_set.s1, copy_set.s2, copy_set.s3, copy_set.s4), start=1):
        assert_safe_subject_spintax(subject, field_name=f"{copy_set.bucket}.s{index}")
    for index, body in enumerate((copy_set.e1, copy_set.e2, copy_set.e3, copy_set.e4), start=1):
        assert_spam_safe(body, field_name=f"{copy_set.bucket}.e{index}")
    return copy_set


def _campaign_template_for_bucket(bucket: str, *, campaign_profile: str) -> RoleBucketCopySet:
    voice = VOICE_FAMILY_BY_BUCKET.get(bucket, "executive_owner")
    audience = BUCKET_DESCRIPTIONS.get(bucket, bucket.replace("_", " ").title())
    role_note = REPORT_ROLE_NOTES.get(voice, REPORT_ROLE_NOTES["executive_owner"])

    if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
        if voice == "leadership":
            return _validated_copy_set(RoleBucketCopySet(
                bucket=bucket,
                audience=audience,
                voice_family=voice,
                role_note=role_note,
                s1="{quiet client risk|client trail gap|delivery blind spot|service drift risk}",
                s2="{ops cleanup drag|shared tool drift|late client surprise|visibility gap}",
                s3="{client proof gap|delivery trail gap|review friction|service drift}",
                s4="{short version|quick outline|right person|worth sending}",
                e1=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We plug in simply and flag drift on the files and paths that matter before it turns into cleanup or a client question.\n\n"
                    "Mind if I send the short version?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e2=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help teams catch drift before that follow-up lands.\n\n"
                    "Worth sending a simple example?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e3=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "Happy to send the path set I would start with.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e4=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "If this sits with someone else, point me there. If not, I can send the short version here.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
            ))

        if voice == "it_ops":
            return _validated_copy_set(RoleBucketCopySet(
                bucket=bucket,
                audience=audience,
                voice_family=voice,
                role_note=role_note,
                s1="{change visibility gap|shared tool drift|client path drift|ops blind spot}",
                s2="{queue cleanup drag|ticket chase|path drift|tooling gap}",
                s3="{shared path drift|what changed|service desk drag|ops signal}",
                s4="{short version|first paths|right person|worth sending}",
                e1=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We stay agentless and flag drift where it actually matters before it turns into queue cleanup.\n\n"
                    "Want the short version?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e2=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help teams cut that chase.\n\n"
                    "Worth sending an example?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e3=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "Happy to send the first path set I'd watch.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e4=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "If this belongs with someone else, point me there. If not, I can send the short version here.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
            ))

        if voice == "security":
            return _validated_copy_set(RoleBucketCopySet(
                bucket=bucket,
                audience=audience,
                voice_family=voice,
                role_note=role_note,
                s1="{evidence trail gap|alert noise drag|review proof gap|control trail gap}",
                s2="{what changed|evidence chase|review trail|security proof}",
                s3="{control drift|evidence review|alert triage|trail quality}",
                s4="{short version|first paths|right person|worth sending}",
                e1=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We flag drift and keep the trail cleaner for reviews and incident follow-up.\n\n"
                    "Want the short version?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e2=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help teams keep that trail tighter.\n\n"
                    "Worth sending an example?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e3=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "Happy to send the first path set I'd review.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e4=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "If this belongs with someone else, point me there. If not, I can send the short version here.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
            ))

        if voice == "commercial":
            return _validated_copy_set(RoleBucketCopySet(
                bucket=bucket,
                audience=audience,
                voice_family=voice,
                role_note=role_note,
                s1="{client trust risk|renewal proof gap|delivery blind spot|account risk}",
                s2="{client follow-up|service proof gap|renewal friction|delivery drag}",
                s3="{delivery proof|client question|trust gap|service quality}",
                s4="{short version|quick outline|right person|worth sending}",
                e1=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help delivery teams catch drift earlier so those conversations stay simpler.\n\n"
                    "Worth a short outline?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e2=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help teams stay ahead of that.\n\n"
                    "Want a simple example?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e3=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "Happy to send the short version.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e4=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "If this belongs with someone else, point me there. If not, I can send the short version here.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
            ))

        if voice == "admin":
            return _validated_copy_set(RoleBucketCopySet(
                bucket=bucket,
                audience=audience,
                voice_family=voice,
                role_note=role_note,
                s1="{follow up drag|review chase gap|documentation drag|coordination risk}",
                s2="{manual chase|review follow-up|documentation gap|coordination drag}",
                s3="{handoff trail|evidence chase|admin review|back and forth}",
                s4="{short version|quick outline|right person|worth sending}",
                e1=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help teams keep a cleaner trail so less needs manual chase.\n\n"
                    "Want the short version?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e2=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "We help lighten that.\n\n"
                    "Worth sending an example?\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e3=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "Happy to send the short version.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
                e4=(
                    "{{first_name}},\n\n"
                    "{{personalization_line}}\n\n"
                    "If this belongs with someone else, point me there. If not, I can send the short version here.\n\n"
                    "Arik Liberman\nFounder, Alertica"
                ),
            ))

    if voice == "technical_it":
        return _validated_copy_set(RoleBucketCopySet(
            bucket=bucket,
            audience=audience,
            voice_family=voice,
            role_note=role_note,
            s1="{quiet file drift|file change risk|ehr drift risk|path drift risk}",
            s2="{audit trail gap|record trail gap|control trail gap|change log gap}",
            s3="{shared path drift|clinic path gap|record path drift|site path drift}",
            s4="{quiet control gap|signal review gap|integrity review gap|record signal gap}",
            e1=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "Reaching out because once cloud EHR, patient portal traffic, shared folders, and report exports are all in the mix, the hard part is keeping a clean change trail without agent sprawl.\n\n"
                "We stay agentless, watch the paths that matter, and send a webhook or email signal the moment a file drifts.\n\n"
                "If useful, I can send a short outline.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e2=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "Lean IT teams usually feel this when someone has to trace what changed, which interface moved it, and whether the export path is still clean.\n\n"
                "We keep watch on those paths so your team has cleaner trail data when review time comes.\n\n"
                "If useful, I can share a simple example.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e3=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "One practical angle here is the first path set to watch. For most clinic teams, that starts with record exports, shared folders, and reporting files.\n\n"
                "If useful, I can sketch the first path set I would start with.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e4=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "If this sits with another owner on your side, I can route a tighter note there. If it stays with you, I can send the short version.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
        ))

    if voice == "finance":
        return _validated_copy_set(RoleBucketCopySet(
            bucket=bucket,
            audience=audience,
            voice_family=voice,
            role_note=role_note,
            s1="{compliance risk|audit cost gap|quiet audit gap|oversight risk}",
            s2="{record risk gap|audit readiness gap|control cost gap|quiet control gap}",
            s3="{reporting drag risk|record review drag|audit review drag|oversight review gap}",
            s4="{board review risk|record oversight gap|audit load signal|budget risk signal}",
            e1=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "Reaching out because OCR never sees the lean team context first. It sees whether the file trail is clean, whether review work stacks up, and how much late cleanup the team is carrying.\n\n"
                "We give IT a direct signal when a file changes, which usually means less audit drag and less budget pressure once review starts.\n\n"
                "If useful, I can send a short outline.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e2=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "The gap usually stays hidden until someone has to explain why a record moved, changed, or showed up late in a review packet.\n\n"
                "We keep watch on the file paths that matter so your team has firmer footing when OCR or internal review starts asking for the trail.\n\n"
                "If useful, I can share a simple example.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e3=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "The part I would look at first is the record flow behind reporting, 340B, and shared exports across sites.\n\n"
                "If useful, I can sketch the first path set I would review.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e4=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "If this sits with another owner on your side, I can route a tighter note there. If it stays with you, I can send the short version.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
        ))

    if voice == "operations":
        return _validated_copy_set(RoleBucketCopySet(
            bucket=bucket,
            audience=audience,
            voice_family=voice,
            role_note=role_note,
            s1="{file security risk|clinic workflow drag|site workflow risk|record drift risk}",
            s2="{quiet reporting drag|cross site drag|shared file drift|ops review gap}",
            s3="{record rework risk|site audit drag|workflow review gap|clinic path drift}",
            s4="{multi site drift|record flow drag|site control gap|path review gap}",
            e1=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "Reaching out because quiet file changes rarely stay quiet for ops. They turn into rerun reports, cross-site cleanup, and staff time spent tracing what moved.\n\n"
                "We watch the file paths that matter and send a signal when something shifts, so clinic teams spend less time chasing the source.\n\n"
                "If useful, I can send a short outline.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e2=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "The drag is rarely the first change itself. It is the clinic-by-clinic cleanup once nobody is sure what moved or when it changed.\n\n"
                "We keep a steady watch on the paths behind reports and shared record flow, which cuts down the hunt.\n\n"
                "If useful, I can share a simple example.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e3=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "One practical starting point is the path set behind reports, exports, and shared folders across sites.\n\n"
                "If useful, I can sketch the first path set I would review.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e4=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "If this sits with another owner on your side, I can route a tighter note there. If it stays with you, I can send the short version.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
        ))

    if voice == "security_compliance_risk":
        return _validated_copy_set(RoleBucketCopySet(
            bucket=bucket,
            audience=audience,
            voice_family=voice,
            role_note=role_note,
            s1="{audit trail gap|control evidence gap|record evidence gap|quiet review gap}",
            s2="{compliance risk|record review risk|control trail gap|integrity review gap}",
            s3="{evidence trail risk|review packet gap|quiet control gap|record trail risk}",
            s4="{file security risk|audit load risk|review drag risk|control proof gap}",
            e1=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "Reaching out because quiet record drift usually shows up first as a weak trail, thin support for review packets, and more time spent proving what changed.\n\n"
                "We watch the file paths that matter and send a signal when something changes, which gives your team a cleaner record trail to work from.\n\n"
                "If useful, I can send a short outline.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e2=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "The gap tends to stay hidden until someone has to show what changed, when it moved, and how the team saw it.\n\n"
                "We keep that trail tighter, which makes review packets less painful to assemble.\n\n"
                "If useful, I can share a simple example.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e3=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "A practical starting point is the file set behind record exports, shared folders, and reporting paths.\n\n"
                "If useful, I can sketch the first path set I would review.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
            e4=(
                "{{first_name}},\n\n"
                "{{personalization_line}}\n\n"
                "If this sits with another owner on your side, I can route a tighter note there. If it stays with you, I can send the short version.\n\n"
                "Arik Liberman\nFounder, Alertica"
            ),
        ))

    return _validated_copy_set(RoleBucketCopySet(
        bucket=bucket,
        audience=audience,
        voice_family=voice,
        role_note=role_note,
        s1="{compliance risk|quiet control gap|record trust gap|audit readiness gap}",
        s2="{board review risk|patient trust gap|record change risk|control evidence gap}",
        s3="{quiet audit gap|record oversight gap|leadership review gap|care continuity risk}",
        s4="{file security risk|record drift risk|audit trail gap|control trail gap}",
        e1=(
            "{{first_name}},\n\n"
            "{{personalization_line}}\n\n"
            "Reaching out because quiet record drift can sit in the background until it starts pulling time from leadership, patient trust, and board-level review.\n\n"
            "We watch the file paths that matter and send a signal when something changes, so the team can review it before it becomes a larger leadership issue.\n\n"
            "If useful, I can send a short outline.\n\n"
            "Arik Liberman\nFounder, Alertica"
        ),
        e2=(
            "{{first_name}},\n\n"
            "{{personalization_line}}\n\n"
            "The gap tends to stay hidden until someone has to explain what changed, how it moved, and why the team saw it late.\n\n"
            "We keep a tighter watch on those paths, which gives the team cleaner footing during review.\n\n"
            "If useful, I can share a simple example.\n\n"
            "Arik Liberman\nFounder, Alertica"
        ),
        e3=(
            "{{first_name}},\n\n"
            "{{personalization_line}}\n\n"
            "A practical first pass is the path set behind record exports, shared folders, and reporting files.\n\n"
            "If useful, I can sketch the first path set I would review.\n\n"
            "Arik Liberman\nFounder, Alertica"
        ),
        e4=(
            "{{first_name}},\n\n"
            "{{personalization_line}}\n\n"
            "If this sits with another owner on your side, I can route a tighter note there. If it stays with you, I can send the short version.\n\n"
            "Arik Liberman\nFounder, Alertica"
        ),
    ))


class CampaignPrepWorkflow:
    def __init__(self, minimax: MiniMaxClient | None = None) -> None:
        self._minimax = minimax

    async def run(
        self,
        *,
        seller: str,
        segment: str,
        date_stamp: str | None = None,
        root: str = "runs",
        policy: CampaignPrepPolicy,
    ) -> CampaignPrepRun:
        paths = build_run_paths(
            seller=seller,
            segment=segment,
            date_stamp=date_stamp,
            root=root,
        )
        scaffold_run_directories(paths)
        report_path = paths.output_dir / f"{paths.naming.run_slug}-spintax-report.md"
        summary_path = paths.output_dir / f"{paths.naming.run_slug}-summary.json"
        normalized_people_path = paths.output_dir / f"{paths.naming.run_slug}-people.csv"
        send_ready_people_path = paths.output_dir / f"{paths.naming.run_slug}-people-send-ready.csv"
        normalized_companies_path = paths.output_dir / f"{paths.naming.run_slug}-companies.csv"
        raw_name_jsonl = paths.raw_dir / f"{paths.naming.run_slug}-minimax-company-name-normalization.jsonl"

        campaign_profile = _resolve_campaign_profile(
            policy_value=policy.campaign_profile,
            seller=seller,
            segment=segment,
        )
        rows = self._read_csv(policy.people_csv)
        filtered_rows = self._filter_rows(
            rows,
            email_only=policy.email_only,
            max_people=policy.max_people,
            campaign_profile=campaign_profile,
        )
        normalizations = await self._normalize_companies(
            rows=filtered_rows,
            use_minimax=policy.use_minimax_name_normalization,
            raw_jsonl_path=raw_name_jsonl,
        )
        generated_first_lines = await self._generate_first_lines(
            rows=filtered_rows,
            normalizations=normalizations,
            use_minimax=policy.use_minimax_personalization,
            campaign_profile=campaign_profile,
        )

        company_rows, person_rows, bucket_counts = self._build_outputs(
            filtered_rows=filtered_rows,
            run_slug=paths.naming.run_slug,
            seller_slug=paths.naming.seller_slug,
            segment_slug=paths.naming.segment_slug,
            normalizations=normalizations,
            generated_first_lines=generated_first_lines,
            campaign_profile=campaign_profile,
        )
        write_person_rows_csv(person_rows, normalized_people_path)
        self._write_send_ready_people_csv(person_rows, send_ready_people_path)
        self._write_company_csv(company_rows, normalized_companies_path)
        report_markdown = self._render_report(
            seller=seller,
            segment=segment,
            run_slug=paths.naming.run_slug,
            person_rows=person_rows,
            company_rows=company_rows,
            bucket_counts=bucket_counts,
            campaign_profile=campaign_profile,
        )
        report_path.write_text(report_markdown, encoding="utf-8")
        summary_payload = {
            "run_slug": paths.naming.run_slug,
            "people_rows": len(person_rows),
            "company_rows": len(company_rows),
            "bucket_counts": bucket_counts,
            "email_only": policy.email_only,
            "campaign_profile": campaign_profile,
            "send_ready_csv": str(send_ready_people_path),
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2) + "\n", encoding="utf-8")
        return CampaignPrepRun(
            run_slug=paths.naming.run_slug,
            people_csv=str(normalized_people_path),
            send_ready_csv=str(send_ready_people_path),
            companies_csv=str(normalized_companies_path),
            report_md=str(report_path),
            summary_json=str(summary_path),
            row_count=len(person_rows),
            company_count=len(company_rows),
            bucket_counts=bucket_counts,
            campaign_profile=campaign_profile,
        )

    @staticmethod
    def _read_csv(path: str) -> list[dict[str, str]]:
        field_limit = sys.maxsize
        while True:
            try:
                csv.field_size_limit(field_limit)
                break
            except OverflowError:
                field_limit //= 10
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def _filter_rows(
        rows: list[dict[str, str]],
        *,
        email_only: bool,
        max_people: int,
        campaign_profile: str,
    ) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for row in rows:
            if not _is_truthy(row.get("person_soft_qualifies") or ""):
                continue
            if email_only and not _clean_text(row.get("person_email")):
                continue
            if not _row_matches_campaign_profile(row, campaign_profile=campaign_profile):
                continue
            filtered.append(row)
        if max_people > 0:
            return filtered[:max_people]
        return filtered

    async def _normalize_companies(
        self,
        *,
        rows: list[dict[str, str]],
        use_minimax: bool,
        raw_jsonl_path: Path,
    ) -> dict[str, CompanyNameNormalization]:
        names: dict[str, tuple[str, str]] = {}
        for row in rows:
            raw_name = (
                _clean_text(row.get("prospeo_company_name"))
                or _clean_text(row.get("source_company_name"))
                or _clean_text(row.get("company_name"))
            )
            if not raw_name:
                continue
            key = raw_name.casefold()
            if key not in names:
                names[key] = (
                    _clean_text(row.get("company_domain")),
                    _clean_text(row.get("company_description")),
                )

        async def worker(item: tuple[str, tuple[str, str]]) -> tuple[str, CompanyNameNormalization]:
            raw_name, (company_domain, company_description) = item
            heuristic = heuristic_normalize_company_name(raw_name)
            if (
                use_minimax
                and self._minimax is not None
                and needs_llm_company_name_cleanup(raw_name)
            ):
                normalizer = MiniMaxCompanyNameNormalizer(self._minimax)
                try:
                    result = await normalizer.normalize(
                        raw_name=raw_name,
                        company_domain=company_domain,
                        company_description=company_description,
                    )
                    raw_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                    with raw_jsonl_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            __import__("json").dumps(
                                {
                                    "raw_name": raw_name,
                                    "company_domain": company_domain,
                                    "method": result.method,
                                    "normalized_name": result.normalized_name,
                                    "short_name": result.short_name,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    return raw_name.casefold(), result
                except Exception:
                    return raw_name.casefold(), heuristic
            return raw_name.casefold(), heuristic

        semaphore = asyncio.Semaphore(3)

        async def limited_worker(item: tuple[str, tuple[str, str]]) -> tuple[str, CompanyNameNormalization]:
            async with semaphore:
                return await worker(item)

        pairs = await asyncio.gather(*(limited_worker(item) for item in names.items()))
        return dict(pairs)

    async def _generate_first_lines(
        self,
        *,
        rows: list[dict[str, str]],
        normalizations: dict[str, CompanyNameNormalization],
        use_minimax: bool,
        campaign_profile: str,
    ) -> dict[int, str]:
        if not use_minimax or self._minimax is None:
            return {}

        writer = MiniMaxPersonalizationWriter(self._minimax)
        semaphore = asyncio.Semaphore(3)
        results: dict[int, str] = {}

        async def worker(index: int, row: dict[str, str]) -> None:
            raw_company_name = (
                _clean_text(row.get("prospeo_company_name"))
                or _clean_text(row.get("source_company_name"))
                or _clean_text(row.get("company_name"))
            )
            normalized = normalizations.get(raw_company_name.casefold()) or heuristic_normalize_company_name(raw_company_name)
            company_name = _copy_company_name(normalized, raw_company_name)
            bucket = _campaign_bucket(row, campaign_profile=campaign_profile)
            voice_bucket = VOICE_FAMILY_BY_BUCKET.get(bucket, "executive_owner")
            role_title = _role_title(row)
            role_description = _role_description(row)
            async with semaphore:
                try:
                    generated = await writer.write_first_line(
                        company_name=company_name,
                        role_title=role_title,
                        role_description=role_description,
                        bucket=voice_bucket,
                        row=row,
                        campaign_profile=campaign_profile,
                    )
                    if generated.p1:
                        assert_spam_safe(generated.p1, field_name=f"{voice_bucket}.p1")
                        results[index] = generated.p1
                except Exception:
                    return

        await asyncio.gather(*(worker(index, row) for index, row in enumerate(rows)))
        return results

    def _build_outputs(
        self,
        *,
        filtered_rows: list[dict[str, str]],
        run_slug: str,
        seller_slug: str,
        segment_slug: str,
        normalizations: dict[str, CompanyNameNormalization],
        generated_first_lines: dict[int, str],
        campaign_profile: str,
    ) -> tuple[list[dict[str, str]], list[PersonRow], dict[str, int]]:
        company_rows_by_key: dict[str, dict[str, str]] = {}
        people_rows: list[PersonRow] = []
        bucket_counts: dict[str, int] = {}

        for row_index, row in enumerate(filtered_rows):
            bucket = _campaign_bucket(row, campaign_profile=campaign_profile)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
            copy_set = _campaign_template_for_bucket(bucket, campaign_profile=campaign_profile)
            raw_company_name = (
                _clean_text(row.get("prospeo_company_name"))
                or _clean_text(row.get("source_company_name"))
                or _clean_text(row.get("company_name"))
            )
            normalized = normalizations.get(raw_company_name.casefold()) or heuristic_normalize_company_name(raw_company_name)
            company_name = _copy_company_name(normalized, raw_company_name)
            role = _role_title(row)
            if not role:
                role = {
                    "leadership": "Leadership",
                    "it_ops": "IT Operations",
                    "security": "Security",
                    "commercial": "Commercial",
                    "admin": "Operations Coordinator",
                }.get(bucket, "")
            role_description = _role_description(row)
            p1, p2, p3, p4 = _personalization_lines(
                bucket=copy_set.voice_family,
                company_name=company_name,
                row=row,
                campaign_profile=campaign_profile,
            )
            if row_index in generated_first_lines:
                p1 = generated_first_lines[row_index]
            for field_index, value in enumerate((p1, p2, p3, p4), start=1):
                assert_spam_safe(value, field_name=f"{bucket}.p{field_index}")
            full_name = (
                _clean_text(row.get("full_name"))
                or _clean_text(row.get("harvest_full_name"))
                or _clean_text(row.get("person_name_guess")).replace("\u200f", "").strip()
            )
            first_name = _clean_text(row.get("first_name"))
            last_name = _clean_text(row.get("last_name"))
            if full_name and (not first_name and not last_name):
                first_name, last_name = _split_full_name(full_name)
            if not full_name:
                full_name = " ".join(part for part in [first_name, last_name] if part).strip()
            person_row = PersonRow(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                company_name=company_name,
                role=role,
                role_description=role_description,
                e1=copy_set.e1,
                e2=copy_set.e2,
                e3=copy_set.e3,
                e4=copy_set.e4,
                s1=copy_set.s1,
                s2=copy_set.s2,
                s3=copy_set.s3,
                s4=copy_set.s4,
                p1=p1,
                p2=p2,
                p3=p3,
                p4=p4,
                run_slug=run_slug,
                seller_slug=seller_slug,
                segment_slug=segment_slug,
                source_role_segment=bucket,
                company_domain=_clean_text(row.get("company_domain") or row.get("prospeo_company_domain") or row.get("source_company_domain")),
                person_linkedin_url=_clean_text(row.get("person_linkedin_url") or row.get("linkedin_profile_url")),
                person_email=_clean_text(row.get("person_email")),
                person_email_status=_clean_text(row.get("person_email_status")),
                person_quality_score="0",
                harvest_profile_id=_clean_text(row.get("person_slug")),
                harvest_status=_clean_text(row.get("harvest_status")),
            )
            people_rows.append(
                PersonRow(
                    **{
                        **person_row.to_storage_dict(),
                        "person_quality_score": str(person_quality_score(person_row)),
                    }
                )
            )

            company_key = (person_row.company_domain or company_name.casefold()).strip().casefold()
            company_row = company_rows_by_key.setdefault(
                company_key,
                {
                    "run_slug": run_slug,
                    "seller_slug": seller_slug,
                    "segment_slug": segment_slug,
                    "company_name_raw": raw_company_name,
                    "company_name": company_name,
                    "company_name_short": _copy_company_name(normalized, normalized.short_name or raw_company_name),
                    "company_name_normalization_method": normalized.method,
                    "company_domain": person_row.company_domain,
                    "company_linkedin_url": _clean_text(row.get("company_linkedin_url")),
                    "company_description": _clean_text(row.get("company_description")),
                    "company_offer": _clean_text(row.get("company_offer")),
                    "company_icp": _clean_text(row.get("company_icp")),
                    "company_painpoint": _clean_text(row.get("company_painpoint")),
                    "company_signals": _clean_text(row.get("company_signals")),
                    "company_signal_sources": _clean_text(row.get("company_signal_sources")),
                    "company_quality_score": _clean_text(row.get("company_quality_score")),
                    "company_needs_followup": _clean_text(row.get("company_needs_followup")),
                    "people_count": "0",
                    "emailed_people_count": "0",
                    "bucket_summary": "",
                },
            )
            people_count = int(company_row["people_count"] or "0") + 1
            emailed_count = int(company_row["emailed_people_count"] or "0") + (1 if person_row.person_email else 0)
            company_row["people_count"] = str(people_count)
            company_row["emailed_people_count"] = str(emailed_count)
            buckets = set(filter(None, [part.strip() for part in company_row["bucket_summary"].split("|")]))
            buckets.add(bucket)
            company_row["bucket_summary"] = " | ".join(sorted(buckets))

        people_rows.sort(key=lambda item: (item.company_name.lower(), item.role.lower(), item.full_name.lower()))
        company_rows = sorted(company_rows_by_key.values(), key=lambda item: (item["company_name"].lower(), item["company_domain"].lower()))
        bucket_counts = dict(sorted(bucket_counts.items(), key=lambda item: (-item[1], item[0])))
        return company_rows, people_rows, bucket_counts

    @staticmethod
    def _write_company_csv(rows: Iterable[dict[str, str]], output_path: str | Path) -> None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        row_list = list(rows)
        headers = [
            "run_slug",
            "seller_slug",
            "segment_slug",
            "company_name_raw",
            "company_name",
            "company_name_short",
            "company_name_normalization_method",
            "company_domain",
            "company_linkedin_url",
            "company_description",
            "company_offer",
            "company_icp",
            "company_painpoint",
            "company_signals",
            "company_signal_sources",
            "company_quality_score",
            "company_needs_followup",
            "people_count",
            "emailed_people_count",
            "bucket_summary",
        ]
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in row_list:
                writer.writerow(row)

    @staticmethod
    def _write_send_ready_people_csv(rows: Iterable[PersonRow], output_path: str | Path) -> None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        row_list = list(rows)
        headers = [
            "full_name",
            "first_name",
            "last_name",
            "company_name",
            "company_domain",
            "role",
            "role_title",
            "role_description",
            "person_email",
            "person_email_status",
            "person_linkedin_url",
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
            "source_role_segment",
            "run_slug",
            "seller_slug",
            "segment_slug",
        ]
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in row_list:
                payload = row.to_storage_dict()
                payload["role_title"] = payload.get("role", "")
                row_seed = "|".join(
                    [
                        payload.get("full_name", ""),
                        payload.get("company_name", ""),
                        payload.get("person_email", ""),
                        payload.get("run_slug", ""),
                    ]
                )
                payload["s1"] = _render_subject_variant(payload.get("s1", ""), row_seed=row_seed, slot="s1")
                payload["s2"] = _render_subject_variant(payload.get("s2", ""), row_seed=row_seed, slot="s2")
                payload["s3"] = _render_subject_variant(payload.get("s3", ""), row_seed=row_seed, slot="s3")
                payload["s4"] = _render_subject_variant(payload.get("s4", ""), row_seed=row_seed, slot="s4")
                payload["e1"] = _render_body_template(
                    payload.get("e1", ""),
                    first_name=payload.get("first_name", ""),
                    personalization_line=payload.get("p1", ""),
                )
                payload["e2"] = _render_body_template(
                    payload.get("e2", ""),
                    first_name=payload.get("first_name", ""),
                    personalization_line=payload.get("p2", ""),
                )
                payload["e3"] = _render_body_template(
                    payload.get("e3", ""),
                    first_name=payload.get("first_name", ""),
                    personalization_line=payload.get("p3", ""),
                )
                payload["e4"] = _render_body_template(
                    payload.get("e4", ""),
                    first_name=payload.get("first_name", ""),
                    personalization_line=payload.get("p4", ""),
                )
                writer.writerow({header: str(payload.get(header, "")) for header in headers})

    def _render_report(
        self,
        *,
        seller: str,
        segment: str,
        run_slug: str,
        person_rows: list[PersonRow],
        company_rows: list[dict[str, str]],
        bucket_counts: dict[str, int],
        campaign_profile: str,
    ) -> str:
        company_lookup = {row["company_domain"].casefold() or row["company_name"].casefold(): row for row in company_rows}
        lines: list[str] = []
        lines.append(f"# {seller.title()} {segment} Spintax Report")
        lines.append("")
        lines.append(f"- Run slug: `{run_slug}`")
        lines.append(f"- People rows: `{len(person_rows)}`")
        lines.append(f"- Companies: `{len(company_rows)}`")
        lines.append(f"- Campaign profile: `{campaign_profile}`")
        if campaign_profile == CAMPAIGN_PROFILE_MSP_MSSP:
            source_note = "filtered MSP/MSSP people with company research context"
        elif campaign_profile == CAMPAIGN_PROFILE_FQHC:
            source_note = "filtered FQHC people with company research and Prospeo email enrichment"
        else:
            source_note = "filtered people with company research context"
        lines.append(f"- Source: {source_note}")
        lines.append("")
        lines.append("## Bucket Mix")
        lines.append("")
        for bucket, count in bucket_counts.items():
            lines.append(f"- `{bucket}`: `{count}`")
        lines.append("")
        lines.append("## Company Name Normalization")
        lines.append("")
        lines.append("Normalized names strip legal suffixes, collapse `dba` variants, trim descriptor appendages, and apply a spam-safe display pass before outreach export.")
        lines.append("")
        lines.append("## Template Rules")
        lines.append("")
        lines.append("- subject variants stay lowercase and stay within 2 to 4 words")
        lines.append("- send-ready exports must materialize `s1..s4` per row; do not rely on Smartlead to parse subject spintax inside custom variables")
        lines.append("- send-ready exports must materialize `e1..e4` per row with the matching `first_name` and `p1..p4` already injected")
        lines.append("- spam-guard rules hard-fail banned words and risky phrases in subject lines, bodies, and personalization")
        lines.append("- `s1/e1`: direct risk angle")
        lines.append("- `s2/e2`: review and oversight angle")
        lines.append("- `s3/e3`: path review angle")
        lines.append("- `s4/e4`: routing angle")
        lines.append("- `p1..p4`: per-account personalization lines built from `company_offer`, `company_icp`, `company_painpoint`, and `company_signals`")
        lines.append("")

        bucket_examples: dict[str, list[PersonRow]] = {}
        for row in person_rows:
            bucket_examples.setdefault(row.source_role_segment, [])
            if len(bucket_examples[row.source_role_segment]) < 3:
                bucket_examples[row.source_role_segment].append(row)

        for bucket, count in bucket_counts.items():
            template = _campaign_template_for_bucket(bucket, campaign_profile=campaign_profile)
            lines.append(f"## {BUCKET_DESCRIPTIONS.get(bucket, bucket.replace('_', ' ').title())}")
            lines.append("")
            lines.append(f"- Bucket key: `{bucket}`")
            lines.append(f"- Rows: `{count}`")
            lines.append(f"- Voice family: `{template.voice_family}`")
            lines.append(f"- Note: {template.role_note}")
            lines.append("")
            lines.append("### Sample Account Context")
            lines.append("")
            for sample in bucket_examples.get(bucket, []):
                lookup_key = sample.company_domain.casefold() or sample.company_name.casefold()
                company = company_lookup.get(lookup_key, {})
                lines.append(f"- **{sample.company_name}**")
                lines.append(f"  Offer: {company.get('company_offer', '')}")
                lines.append(f"  ICP: {company.get('company_icp', '')}")
                lines.append(f"  Painpoint: {company.get('company_painpoint', '')}")
                lines.append(f"  Signals: {company.get('company_signals', '')}")
                lines.append(f"  Example role: {sample.role}")
            lines.append("")
            lines.append("### Subject Variants")
            lines.append("")
            for index, value in enumerate((template.s1, template.s2, template.s3, template.s4), start=1):
                lines.append(f"#### s{index}")
                lines.append("")
                lines.append("```txt")
                lines.append(value)
                lines.append("```")
                lines.append("")
            lines.append("### Personalization Variants")
            lines.append("")
            example_row = bucket_examples.get(bucket, [None])[0]
            if example_row is not None:
                example_values = [example_row.p1, example_row.p2, example_row.p3, example_row.p4]
            else:
                example_values = ["", "", "", ""]
            for index, value in enumerate(example_values, start=1):
                lines.append(f"#### p{index}")
                lines.append("")
                lines.append("```txt")
                lines.append(value)
                lines.append("```")
                lines.append("")
            lines.append("### Email Variants")
            lines.append("")
            for index, value in enumerate((template.e1, template.e2, template.e3, template.e4), start=1):
                lines.append(f"#### e{index}")
                lines.append("")
                lines.append("```txt")
                lines.append(value)
                lines.append("```")
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"
