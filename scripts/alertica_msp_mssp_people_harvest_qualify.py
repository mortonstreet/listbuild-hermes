#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from listbuild.config import ProviderSettings, load_local_env, load_settings
from listbuild.jsonl import append_jsonl
from listbuild.providers import HarvestClient, MiniMaxClient


DEFAULT_CANDIDATES_CSV = "/Users/mortonstreet/alertica/alertica-tiers/alertica_msp_mssp_51-200_201-500_people_serper_candidates.csv"
DEFAULT_OUTPUT_DIR = "/Users/mortonstreet/alertica/alertica-tiers"
DEFAULT_BASE_NAME = "alertica_msp_mssp_51-200_201-500_people_harvest_qualifier"
HARVEST_FULL_PROFILE_USD = 0.0032
ADJACENT_MINIMAX_MODEL = os.environ.get("MINIMAX_ADJACENT_MODEL", "MiniMax-Text-01").strip() or "MiniMax-Text-01"
ADJACENT_PREFILTER_PATTERNS: tuple[str, ...] = (
    r"\bcio\b",
    r"\bchief information officer\b",
    r"\bcto\b",
    r"\bchief technology officer\b",
    r"\bvp\b",
    r"\bvice president\b",
    r"\bhead of\b",
    r"\bdirector\b",
    r"\bmanager\b",
    r"\blead\b",
    r"\bsecurity\b",
    r"\bcyber\b",
    r"\bsoc\b",
    r"\binfrastructure\b",
    r"\bcloud\b",
    r"\bdevops\b",
    r"\bsystem engineer\b",
    r"\bsystems engineer\b",
    r"\bsystem administrator\b",
    r"\bsystems administrator\b",
    r"\bnetwork administrator\b",
    r"\boperations\b",
    r"\bservice delivery\b",
    r"\bservice desk\b",
    r"\baccount\b",
    r"\bchannel\b",
    r"\bpartner\b",
    r"\bcustomer success\b",
    r"\bclient success\b",
    r"\bbusiness development\b",
    r"\bsales\b",
    r"\blegal\b",
    r"\bcompliance\b",
    r"\bexecutive assistant\b",
    r"\boffice manager\b",
    r"\bowner\b",
    r"\bfounder\b",
    r"\bpresident\b",
    r"\bchief\b",
)


@dataclass(frozen=True, slots=True)
class TitleRule:
    title: str
    role_segment: str
    priority: int
    patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdjacentReasoningResult:
    adjacent_fit: bool
    mapped_title: str
    mapped_role_segment: str
    confidence: str
    reason: str
    raw_response: dict[str, Any]
    parsed_response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AdjacentOverrideRule:
    title: str
    role_segment: str
    priority: int
    reason: str
    patterns: tuple[str, ...]


TITLE_RULES: tuple[TitleRule, ...] = (
    TitleRule("CEO", "leadership", 10, (r"\bceo\b", r"\bchief executive officer\b")),
    TitleRule("President", "leadership", 11, (r"\bpresident\b",)),
    TitleRule("Owner", "leadership", 12, (r"\bowner\b",)),
    TitleRule("Founder", "leadership", 13, (r"\bfounder\b",)),
    TitleRule("Co-founder", "leadership", 14, (r"\bco[\s-]?founder\b",)),
    TitleRule("COO", "leadership", 15, (r"\bcoo\b", r"\bchief operating officer\b")),
    TitleRule("CFO", "leadership", 16, (r"\bcfo\b", r"\bchief financial officer\b")),
    TitleRule("CMO", "leadership", 17, (r"\bcmo\b", r"\bchief marketing officer\b")),
    TitleRule(
        "IT Manager",
        "it_ops",
        30,
        (r"\bit manager\b", r"\binformation technology manager\b"),
    ),
    TitleRule(
        "IT Operations",
        "it_ops",
        31,
        (
            r"\bit operations\b",
            r"\bit ops\b",
            r"\bit operations manager\b",
            r"\bdirector of it operations\b",
            r"\bhead of it operations\b",
        ),
    ),
    TitleRule(
        "System Administrator",
        "it_ops",
        32,
        (r"\bsystem administrator\b", r"\bsystems administrator\b", r"\bsys[\s-]?admin\b"),
    ),
    TitleRule(
        "Infrastructure Engineer",
        "it_ops",
        33,
        (r"\binfrastructure engineer\b", r"\bcloud infrastructure engineer\b"),
    ),
    TitleRule(
        "Security Manager",
        "security",
        40,
        (
            r"\bsecurity manager\b",
            r"\binformation security manager\b",
            r"\bcybersecurity manager\b",
        ),
    ),
    TitleRule(
        "Security Engineer",
        "security",
        41,
        (
            r"\bsecurity engineer\b",
            r"\binformation security engineer\b",
            r"\bcybersecurity engineer\b",
        ),
    ),
    TitleRule(
        "Security Analyst",
        "security",
        42,
        (
            r"\bsecurity analyst\b",
            r"\binformation security analyst\b",
            r"\bcybersecurity analyst\b",
            r"\bsoc analyst\b",
        ),
    ),
    TitleRule(
        "Business Development",
        "commercial",
        50,
        (
            r"\bbusiness development\b",
            r"\bbusiness development manager\b",
            r"\bdirector of business development\b",
        ),
    ),
    TitleRule("Account Manager", "commercial", 51, (r"\baccount manager\b",)),
    TitleRule("Administrative Assistant", "admin", 60, (r"\badministrative assistant\b",)),
)


ADJACENT_OVERRIDE_RULES: tuple[AdjacentOverrideRule, ...] = (
    AdjacentOverrideRule(
        title="Legal Director",
        role_segment="security",
        priority=43,
        reason="legal leadership can be a worthwhile contact when the outreach is framed around security and compliance risk",
        patterns=(r"\blegal director\b",),
    ),
    AdjacentOverrideRule(
        title="Global Head of Brands & Legal",
        role_segment="security",
        priority=43,
        reason="brands and legal leadership can matter when the outreach is framed around security, compliance, and risk exposure",
        patterns=(r"\bglobal head of brands\s*&\s*legal\b", r"\bhead of brands\s*&\s*legal\b"),
    ),
    AdjacentOverrideRule(
        title="Executive Assistant & Office Manager",
        role_segment="admin",
        priority=59,
        reason="executive assistant and office manager roles can be worthwhile contacts for security and compliance coordination inside smaller service providers",
        patterns=(
            r"\bexecutive assistant\s*&\s*office manager\b",
            r"\boffice manager\b.*\bexecutive assistant\b",
            r"\bexecutive assistant\b.*\boffice manager\b",
        ),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify Serper-discovered MSP/MSSP people with Harvest full profile lookups, "
            "using current employer and tracked title matching only."
        )
    )
    parser.add_argument(
        "--candidates-csv",
        default=DEFAULT_CANDIDATES_CSV,
        help="Serper people candidate CSV.",
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
        "--harvest-concurrency",
        type=int,
        default=6,
        help="Concurrent Harvest profile requests.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        help="Retries per profile for transient network failures.",
    )
    parser.add_argument(
        "--refresh-profiles",
        action="store_true",
        help="Ignore cached profile responses and fetch every profile again.",
    )
    parser.add_argument(
        "--max-profiles",
        type=int,
        default=0,
        help="Optional cap on unresolved profile URLs to process in this run.",
    )
    parser.add_argument(
        "--skip-harvest-fetch",
        action="store_true",
        help="Use cached Harvest profile results only and do not fetch unresolved profiles in this run.",
    )
    parser.add_argument(
        "--use-minimax-adjacent",
        action="store_true",
        help="Use MiniMax reasoning to rescue adjacent-fit titles when employer matches but exact title rules do not.",
    )
    parser.add_argument(
        "--max-adjacent-reasoning",
        type=int,
        default=0,
        help="Optional cap on adjacent title reasoning calls for this run. 0 means no cap.",
    )
    parser.add_argument(
        "--adjacent-reasoning-concurrency",
        type=int,
        default=2,
        help="Concurrent MiniMax adjacent-fit reasoning calls.",
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


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def normalize_text(value: str) -> str:
    lowered = clean_text(value).lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(collapsed.split())


def extract_json_object(text: str) -> dict[str, Any] | None:
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


def normalize_adjacent_parsed_response(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    wrapped = payload.get("adjacent_fit_result")
    if isinstance(wrapped, dict):
        return wrapped
    return payload


def message_content(payload: dict[str, Any]) -> str:
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


def message_reasoning_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("reasoning_content")
    if isinstance(content, str):
        return content
    return ""


def company_match(expected: str, observed: str) -> bool:
    left = normalize_text(expected)
    right = normalize_text(observed)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def extract_current_company(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    current_position = element.get("currentPosition")
    if isinstance(current_position, list) and current_position:
        first = current_position[0]
        if isinstance(first, dict):
            company_name = clean_text(first.get("companyName"))
            if company_name:
                return company_name
    current_company = element.get("currentCompany")
    if isinstance(current_company, dict):
        company_name = clean_text(current_company.get("name"))
        if company_name:
            return company_name
    return ""


def extract_full_name(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    first_name = clean_text(element.get("firstName"))
    last_name = clean_text(element.get("lastName"))
    return " ".join(part for part in (first_name, last_name) if part).strip()


def extract_headline(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    return clean_text(element.get("headline"))


def extract_about(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    return clean_text(element.get("about"))


def extract_location(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    location = element.get("location")
    if isinstance(location, dict):
        return clean_text(location.get("linkedinText"))
    return ""


def extract_profile_id(payload: dict[str, Any]) -> str:
    element = payload.get("element")
    if not isinstance(element, dict):
        return ""
    return clean_text(element.get("id"))


def match_title_rules(headline: str) -> list[TitleRule]:
    matched: list[TitleRule] = []
    for rule in TITLE_RULES:
        if any(re.search(pattern, headline, flags=re.IGNORECASE) for pattern in rule.patterns):
            matched.append(rule)
    matched.sort(key=lambda rule: (rule.priority, rule.title))
    return matched


def title_rule_lookup() -> dict[str, TitleRule]:
    return {rule.title.casefold(): rule for rule in TITLE_RULES}


def match_adjacent_override(headline: str) -> AdjacentOverrideRule | None:
    matched: list[AdjacentOverrideRule] = []
    for rule in ADJACENT_OVERRIDE_RULES:
        if any(re.search(pattern, headline, flags=re.IGNORECASE) for pattern in rule.patterns):
            matched.append(rule)
    if not matched:
        return None
    matched.sort(key=lambda rule: (rule.priority, rule.title))
    return matched[0]


def adjacent_prefilter(headline: str) -> bool:
    text = clean_text(headline)
    if not text:
        return False
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in ADJACENT_PREFILTER_PATTERNS)


def adjacent_reasoning_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            clean_text(row.get("company_key")),
            clean_text(row.get("linkedin_profile_url")),
            clean_text(row.get("harvest_headline")),
        ]
    )


def parse_min_serper_rank(row: dict[str, str]) -> int:
    try:
        payload = json.loads(row.get("serper_hits_json") or "[]")
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        return 999
    ranks = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            ranks.append(int(item.get("rank") or 999))
        except ValueError:
            continue
    return min(ranks) if ranks else 999


def dedupe_profile_urls(rows: list[dict[str, str]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        linkedin_url = clean_text(row.get("linkedin_profile_url"))
        if not linkedin_url or linkedin_url in seen:
            continue
        seen.add(linkedin_url)
        ordered.append(linkedin_url)
    return ordered


def load_existing_profile_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    results: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            linkedin_url = clean_text(payload.get("linkedin_profile_url"))
            if not linkedin_url:
                continue
            response = payload.get("response")
            error = clean_text(payload.get("error"))
            if isinstance(response, dict):
                results[linkedin_url] = {
                    "error": "",
                    "status_code": int(payload.get("status_code") or 0),
                    "payload": response,
                }
                continue
            if linkedin_url not in results:
                results[linkedin_url] = {
                    "error": error or "unknown error",
                    "status_code": int(payload.get("status_code") or 0),
                    "payload": {},
                }
    return results


def load_existing_adjacent_results(path: Path) -> dict[str, AdjacentReasoningResult]:
    if not path.exists():
        return {}
    results: dict[str, AdjacentReasoningResult] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            cache_key = clean_text(payload.get("adjacent_reasoning_key"))
            parsed = normalize_adjacent_parsed_response(payload.get("parsed_response"))
            adjacent_fit = clean_text(parsed.get("adjacent_fit")) if isinstance(parsed, dict) else ""
            if not cache_key or not isinstance(parsed, dict) or adjacent_fit.lower() not in {"yes", "no"}:
                continue
            results[cache_key] = AdjacentReasoningResult(
                adjacent_fit=adjacent_fit.lower() == "yes",
                mapped_title=clean_text(parsed.get("mapped_title")),
                mapped_role_segment=clean_text(parsed.get("mapped_role_segment")),
                confidence=clean_text(parsed.get("confidence")),
                reason=clean_text(parsed.get("reason")),
                raw_response=payload.get("raw_response") if isinstance(payload.get("raw_response"), dict) else {},
                parsed_response=parsed,
            )
    return results


async def fetch_profiles(
    *,
    profile_urls: list[str],
    harvest: HarvestClient,
    concurrency: int,
    profiles_jsonl_path: Path,
    max_attempts: int,
    existing_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, dict[str, Any]] = dict(existing_results or {})
    results_lock = asyncio.Lock()
    pending_urls = [
        linkedin_url
        for linkedin_url in profile_urls
        if linkedin_url not in results or clean_text(results[linkedin_url].get("error"))
    ]

    async def worker(linkedin_url: str) -> None:
        async with semaphore:
            last_error = ""
            for attempt in range(1, max(max_attempts, 1) + 1):
                try:
                    response = await harvest.get_profile(
                        url=linkedin_url,
                        include_about_profile=True,
                    )
                    payload = {
                        "provider": response.provider,
                        "status_code": response.status_code,
                        "rate_limit": asdict(response.rate_limit),
                        "linkedin_profile_url": linkedin_url,
                        "attempt": attempt,
                        "response": response.data,
                    }
                    append_jsonl(profiles_jsonl_path, payload)
                    async with results_lock:
                        results[linkedin_url] = {
                            "error": "",
                            "status_code": response.status_code,
                            "payload": response.data if isinstance(response.data, dict) else {},
                        }
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    if attempt < max(max_attempts, 1):
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
                        continue
                    append_jsonl(
                        profiles_jsonl_path,
                        {
                            "provider": "harvest",
                            "linkedin_profile_url": linkedin_url,
                            "attempt": attempt,
                            "error": last_error,
                        },
                    )
                    async with results_lock:
                        results[linkedin_url] = {
                            "error": last_error,
                            "status_code": 0,
                            "payload": {},
                        }

    await asyncio.gather(*(worker(linkedin_url) for linkedin_url in pending_urls))
    return results


async def reason_adjacent_titles(
    *,
    rows: list[dict[str, Any]],
    minimax: MiniMaxClient,
    concurrency: int,
    max_reasoning: int,
    adjacent_jsonl_path: Path,
    existing_results: dict[str, AdjacentReasoningResult] | None = None,
) -> tuple[dict[str, AdjacentReasoningResult], int]:
    results: dict[str, AdjacentReasoningResult] = dict(existing_results or {})
    semaphore = asyncio.Semaphore(concurrency)
    results_lock = asyncio.Lock()
    pending_rows: list[dict[str, Any]] = []
    for row in rows:
        cache_key = adjacent_reasoning_key(row)
        if not cache_key or cache_key in results:
            continue
        pending_rows.append(row)
    if max_reasoning > 0:
        pending_rows = pending_rows[:max_reasoning]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "adjacent_fit_result",
            "schema": {
                "type": "object",
                "properties": {
                    "adjacent_fit": {"type": "string", "enum": ["yes", "no"]},
                    "mapped_title": {"type": "string"},
                    "mapped_role_segment": {
                        "type": "string",
                        "enum": ["leadership", "it_ops", "security", "commercial", "admin", ""],
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": [
                    "adjacent_fit",
                    "mapped_title",
                    "mapped_role_segment",
                    "confidence",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
    }

    async def worker(row: dict[str, Any]) -> None:
        cache_key = adjacent_reasoning_key(row)
        async with semaphore:
            response = await minimax.chat_completion(
                model=ADJACENT_MINIMAX_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify whether an employer-matched contact is worth keeping for MSP/MSSP outbound. "
                            "Prefer concise classification over explanation. "
                            "Keep adjacent leadership, IT, security, infrastructure, service delivery, managed services, operations, account, partner, channel, and business development roles when they plausibly influence tooling, delivery, client security, or buying. "
                            "Also keep legal/compliance leadership roles and executive assistant or office manager roles when the outreach angle is security, compliance, or risk coordination. "
                            "Reject clearly unrelated HR, finance-only, recruiting, and logistics roles."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task": "Return the classification object only.",
                                "tracked_titles": [rule.title for rule in TITLE_RULES],
                                "headline": clean_text(row.get("harvest_headline")),
                                "company_name": clean_text(row.get("company_name")),
                                "company_domain": clean_text(row.get("company_domain")),
                                "requested_titles": clean_text(row.get("requested_titles")),
                                "source_role_segments": clean_text(row.get("source_role_segments")),
                                "serper_title": clean_text(row.get("best_serper_title")),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.0,
                max_completion_tokens=180,
                response_format=response_format,
            )
            payload = response.data if isinstance(response.data, dict) else {}
            parsed = normalize_adjacent_parsed_response(extract_json_object(message_content(payload)) or {})
            result = AdjacentReasoningResult(
                adjacent_fit=clean_text(parsed.get("adjacent_fit")).lower() == "yes",
                mapped_title=clean_text(parsed.get("mapped_title")),
                mapped_role_segment=clean_text(parsed.get("mapped_role_segment")),
                confidence=clean_text(parsed.get("confidence")),
                reason=clean_text(parsed.get("reason")),
                raw_response=payload,
                parsed_response=parsed,
            )
            append_jsonl(
                adjacent_jsonl_path,
                {
                    "adjacent_reasoning_key": cache_key,
                    "company_name": clean_text(row.get("company_name")),
                    "linkedin_profile_url": clean_text(row.get("linkedin_profile_url")),
                    "harvest_headline": clean_text(row.get("harvest_headline")),
                    "reasoning_content": message_reasoning_content(payload),
                    "raw_response": payload,
                    "parsed_response": parsed,
                },
            )
            async with results_lock:
                results[cache_key] = result

    await asyncio.gather(*(worker(row) for row in pending_rows))
    return results, len(pending_rows)


def score_rows(
    rows: list[dict[str, str]],
    profile_lookup: dict[str, dict[str, Any]],
    adjacent_results: dict[str, AdjacentReasoningResult] | None = None,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    title_rules = title_rule_lookup()
    for row in rows:
        linkedin_url = clean_text(row.get("linkedin_profile_url"))
        result = profile_lookup.get(linkedin_url, {"error": "missing profile payload", "payload": {}})
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        harvest_error = clean_text(result.get("error"))
        harvest_headline = extract_headline(payload)
        matched_rules = match_title_rules(harvest_headline)
        adjacent_override = match_adjacent_override(harvest_headline)
        expected_company = clean_text(row.get("company_name"))
        harvest_current_company = extract_current_company(payload)
        employer_match = company_match(expected_company, harvest_current_company)
        exact_title_match = bool(matched_rules)
        adjacent_result = None
        if (
            not harvest_error
            and employer_match
            and not exact_title_match
            and adjacent_override is None
            and adjacent_results is not None
        ):
            adjacent_result = adjacent_results.get(adjacent_reasoning_key(
                {
                    **row,
                    "harvest_headline": harvest_headline,
                }
            ))
        title_match = bool(
            exact_title_match
            or adjacent_override is not None
            or (adjacent_result and adjacent_result.adjacent_fit)
        )
        if exact_title_match:
            matched_titles = [rule.title for rule in matched_rules]
            matched_role_segments = sorted({rule.role_segment for rule in matched_rules})
            primary_rule = matched_rules[0]
            title_match_source = "exact"
        elif adjacent_override is not None:
            matched_titles = [adjacent_override.title]
            matched_role_segments = [adjacent_override.role_segment]
            primary_rule = None
            title_match_source = "adjacent_override"
        elif adjacent_result and adjacent_result.adjacent_fit:
            mapped_rule = title_rules.get(adjacent_result.mapped_title.casefold())
            matched_titles = [adjacent_result.mapped_title] if adjacent_result.mapped_title else []
            matched_role_segments = [adjacent_result.mapped_role_segment] if adjacent_result.mapped_role_segment else []
            primary_rule = mapped_rule
            title_match_source = "minimax_adjacent"
        else:
            matched_titles = []
            matched_role_segments = []
            primary_rule = None
            title_match_source = "none"
        qualifies = bool(not harvest_error and employer_match and title_match)
        min_rank = parse_min_serper_rank(row)
        scored.append(
            {
                **row,
                "min_serper_rank": str(min_rank),
                "harvest_status": clean_text(payload.get("status")) or (f"error: {harvest_error}" if harvest_error else ""),
                "harvest_profile_id": extract_profile_id(payload),
                "harvest_full_name": extract_full_name(payload),
                "harvest_headline": harvest_headline,
                "harvest_about": extract_about(payload),
                "harvest_current_company": harvest_current_company,
                "harvest_location": extract_location(payload),
                "current_employer_match": "yes" if employer_match else "no",
                "title_match": "yes" if title_match else "no",
                "title_match_source": title_match_source,
                "matched_titles": " | ".join(matched_titles),
                "matched_role_segments": " | ".join(matched_role_segments),
                "primary_matched_title": (
                    primary_rule.title
                    if primary_rule
                    else (
                        adjacent_override.title
                        if adjacent_override is not None
                        else (adjacent_result.mapped_title if adjacent_result and adjacent_result.adjacent_fit else "")
                    )
                ),
                "primary_matched_role_segment": (
                    primary_rule.role_segment
                    if primary_rule
                    else (
                        adjacent_override.role_segment
                        if adjacent_override is not None
                        else (adjacent_result.mapped_role_segment if adjacent_result and adjacent_result.adjacent_fit else "")
                    )
                ),
                "title_priority": str(
                    primary_rule.priority
                    if primary_rule
                    else (adjacent_override.priority if adjacent_override is not None else 999)
                ),
                "adjacent_fit": "yes" if adjacent_override is not None or (adjacent_result and adjacent_result.adjacent_fit) else "no",
                "adjacent_fit_confidence": (
                    "high"
                    if adjacent_override is not None
                    else (adjacent_result.confidence if adjacent_result else "")
                ),
                "adjacent_fit_reason": (
                    adjacent_override.reason
                    if adjacent_override is not None
                    else (adjacent_result.reason if adjacent_result else "")
                ),
                "person_soft_qualifies": "yes" if qualifies else "no",
                "person_soft_qualifier_reason": (
                    "current employer and tracked title match"
                    if qualifies and title_match_source == "exact"
                    else (
                        "current employer matched and the title is explicitly allowed for security or compliance risk flagging"
                        if qualifies and title_match_source == "adjacent_override"
                        else (
                        "current employer matched and MiniMax accepted the title as an adjacent fit"
                        if qualifies and title_match_source == "minimax_adjacent"
                        else ("harvest error" if harvest_error else "review employer or title")
                        )
                    )
                ),
                "harvest_json": json.dumps(payload, ensure_ascii=False) if payload else "",
            }
        )
    return scored


def select_final_people(scored_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in scored_rows:
        if clean_text(row.get("person_soft_qualifies")).lower() != "yes":
            continue
        company_key = clean_text(row.get("company_key"))
        grouped.setdefault(company_key, []).append(row)

    selected: list[dict[str, Any]] = []
    for company_key, rows in grouped.items():
        rows.sort(
            key=lambda row: (
                int(row.get("title_priority") or 999),
                int(row.get("min_serper_rank") or 999),
                -int(row.get("serper_hit_count") or 0),
                clean_text(row.get("harvest_full_name") or row.get("person_name_guess")).lower(),
                clean_text(row.get("linkedin_profile_url")).lower(),
            )
        )
        try:
            cap = int(clean_text(rows[0].get("target_people_cap")) or "0")
        except ValueError:
            cap = 0
        for index, row in enumerate(rows[:cap], start=1):
            selected.append(
                {
                    **row,
                    "selection_rank_within_company": str(index),
                    "selected_for_final_list": "yes",
                }
            )
    selected.sort(
        key=lambda row: (
            clean_text(row.get("company_name")).lower(),
            int(row.get("selection_rank_within_company") or 999),
            int(row.get("title_priority") or 999),
        )
    )
    return selected


async def run(args: argparse.Namespace) -> dict[str, Any]:
    candidates_csv = Path(args.candidates_csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_qualified_csv = output_dir / f"{args.base_name}_all.csv"
    final_qualified_csv = output_dir / f"{args.base_name}_final.csv"
    profiles_jsonl_path = output_dir / f"{args.base_name}_profiles.jsonl"
    adjacent_jsonl_path = output_dir / f"{args.base_name}_minimax_adjacent.jsonl"
    summary_path = output_dir / f"{args.base_name}_summary.json"

    for path in (all_qualified_csv, final_qualified_csv, summary_path):
        if path.exists():
            path.unlink()
    if args.refresh_profiles and profiles_jsonl_path.exists():
        profiles_jsonl_path.unlink()

    candidate_rows = read_csv(candidates_csv)
    unique_profile_urls = dedupe_profile_urls(candidate_rows)
    existing_results = {} if args.refresh_profiles else load_existing_profile_results(profiles_jsonl_path)
    pending_urls = [
        linkedin_url
        for linkedin_url in unique_profile_urls
        if linkedin_url not in existing_results or clean_text(existing_results[linkedin_url].get("error"))
    ]
    if args.max_profiles > 0:
        pending_urls = pending_urls[: args.max_profiles]
    if args.skip_harvest_fetch:
        profile_lookup = dict(existing_results)
        pending_urls = []
    else:
        harvest_settings = load_harvest_settings()
        async with HarvestClient(harvest_settings) as harvest:
            profile_lookup = await fetch_profiles(
                profile_urls=pending_urls,
                harvest=harvest,
                concurrency=args.harvest_concurrency,
                profiles_jsonl_path=profiles_jsonl_path,
                max_attempts=args.max_attempts,
                existing_results=existing_results,
            )

    adjacent_existing_results = load_existing_adjacent_results(adjacent_jsonl_path)
    adjacent_prefilter_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        linkedin_url = clean_text(row.get("linkedin_profile_url"))
        result = profile_lookup.get(linkedin_url, {"error": "missing profile payload", "payload": {}})
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        harvest_error = clean_text(result.get("error"))
        harvest_headline = extract_headline(payload)
        employer_match = company_match(clean_text(row.get("company_name")), extract_current_company(payload))
        exact_title_match = bool(match_title_rules(harvest_headline))
        if harvest_error or not employer_match or exact_title_match:
            continue
        if not adjacent_prefilter(harvest_headline):
            continue
        adjacent_prefilter_rows.append(
            {
                **row,
                "harvest_headline": harvest_headline,
                "harvest_current_company": extract_current_company(payload),
            }
        )

    adjacent_results: dict[str, AdjacentReasoningResult] = dict(adjacent_existing_results)
    adjacent_reasoning_requests = 0
    if args.use_minimax_adjacent and adjacent_prefilter_rows:
        settings = load_settings()
        if settings.minimax is None:
            raise RuntimeError("MiniMax is not configured but --use-minimax-adjacent was requested")
        async with MiniMaxClient(settings.minimax) as minimax:
            adjacent_results, adjacent_reasoning_requests = await reason_adjacent_titles(
                rows=adjacent_prefilter_rows,
                minimax=minimax,
                concurrency=args.adjacent_reasoning_concurrency,
                max_reasoning=args.max_adjacent_reasoning,
                adjacent_jsonl_path=adjacent_jsonl_path,
                existing_results=adjacent_existing_results,
            )

    scored_rows = score_rows(candidate_rows, profile_lookup, adjacent_results=adjacent_results)
    final_rows = select_final_people(scored_rows)

    all_fieldnames = [
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
        "min_serper_rank",
        "first_seen_at",
        "best_serper_title",
        "best_serper_snippet",
        "company_description",
        "company_offer",
        "company_icp",
        "company_painpoint",
        "company_signals",
        "company_signal_sources",
        "harvest_status",
        "harvest_profile_id",
        "harvest_full_name",
        "harvest_headline",
        "harvest_about",
        "harvest_current_company",
        "harvest_location",
        "current_employer_match",
        "title_match",
        "title_match_source",
        "matched_titles",
        "matched_role_segments",
        "primary_matched_title",
        "primary_matched_role_segment",
        "title_priority",
        "adjacent_fit",
        "adjacent_fit_confidence",
        "adjacent_fit_reason",
        "person_soft_qualifies",
        "person_soft_qualifier_reason",
        "serper_hits_json",
        "harvest_json",
    ]
    final_fieldnames = [
        *all_fieldnames,
        "selection_rank_within_company",
        "selected_for_final_list",
    ]
    write_csv(all_qualified_csv, scored_rows, all_fieldnames)
    write_csv(final_qualified_csv, final_rows, final_fieldnames)

    profile_errors = sum(1 for result in profile_lookup.values() if clean_text(result.get("error")))
    qualified_rows = sum(1 for row in scored_rows if clean_text(row.get("person_soft_qualifies")).lower() == "yes")
    adjacent_qualified_rows = sum(
        1
        for row in scored_rows
        if clean_text(row.get("person_soft_qualifies")).lower() == "yes"
        and clean_text(row.get("title_match_source")) == "minimax_adjacent"
    )
    companies_with_qualified_people = len(
        {
            clean_text(row.get("company_key"))
            for row in scored_rows
            if clean_text(row.get("person_soft_qualifies")).lower() == "yes"
        }
    )
    companies_meeting_cap = 0
    qualified_count_by_company: dict[str, int] = {}
    cap_by_company: dict[str, int] = {}
    for row in scored_rows:
        company_key = clean_text(row.get("company_key"))
        if not company_key:
            continue
        if company_key not in cap_by_company:
            try:
                cap_by_company[company_key] = int(clean_text(row.get("target_people_cap")) or "0")
            except ValueError:
                cap_by_company[company_key] = 0
        if clean_text(row.get("person_soft_qualifies")).lower() == "yes":
            qualified_count_by_company[company_key] = qualified_count_by_company.get(company_key, 0) + 1
    for company_key, cap in cap_by_company.items():
        if qualified_count_by_company.get(company_key, 0) >= cap > 0:
            companies_meeting_cap += 1

    summary = {
        "candidate_rows": len(candidate_rows),
        "unique_profile_urls": len(unique_profile_urls),
        "harvest_profile_calls": len(unique_profile_urls),
        "estimated_harvest_cost_usd": round(len(unique_profile_urls) * HARVEST_FULL_PROFILE_USD, 2),
        "cached_success_profiles": sum(
            1 for result in existing_results.values() if not clean_text(result.get("error"))
        ),
        "requested_profiles_this_run": len(pending_urls),
        "harvest_fetch_skipped": args.skip_harvest_fetch,
        "adjacent_prefilter_rows": len(adjacent_prefilter_rows),
        "cached_adjacent_results": len(adjacent_existing_results),
        "adjacent_reasoning_requests_this_run": adjacent_reasoning_requests,
        "profile_errors": profile_errors,
        "qualified_rows": qualified_rows,
        "adjacent_qualified_rows": adjacent_qualified_rows,
        "companies_with_qualified_people": companies_with_qualified_people,
        "companies_meeting_people_cap": companies_meeting_cap,
        "final_selected_rows": len(final_rows),
        "output_files": {
            "all_qualified_csv": str(all_qualified_csv),
            "final_qualified_csv": str(final_qualified_csv),
            "profiles_jsonl": str(profiles_jsonl_path),
            "adjacent_jsonl": str(adjacent_jsonl_path),
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
