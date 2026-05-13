from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from listbuild.providers import MiniMaxClient


_LEGAL_SUFFIX_PATTERNS = (
    r",?\s+incorporated\.?$",
    r",?\s+inc\.?$",
    r",?\s+llc\.?$",
    r",?\s+llp\.?$",
    r",?\s+ltd\.?$",
    r",?\s+corp\.?$",
    r",?\s+corporation\.?$",
    r",?\s+co\.?$",
    r",?\s+company\.?$",
    r",?\s+pllc\.?$",
    r",?\s+p\.?c\.?$",
    r",?\s+p\.?a\.?$",
    r",?\s+pbc\.?$",
)

_DASH_DESCRIPTOR_HINTS = (
    "recruiting",
    "staffing",
    "business services",
    "executive recruiting",
    "consulting",
    "advisory",
    "outsourcing",
)

_PARENTHETICAL_DESCRIPTOR_HINTS = (
    "technology",
    "tech",
    "consulting",
    "services",
    "solutions",
    "group",
    "company",
    "co.",
    "advisory",
    "outsourcing",
    "business services",
)

_GENERIC_TRAILING_TOKENS = {
    "solutions",
    "solution",
    "services",
    "service",
    "group",
    "consulting",
    "technologies",
    "technology",
    "systems",
    "advisory",
    "advisors",
    "partners",
    "partner",
}

_UPPERCASE_KEEPERS = {
    "FQHC",
    "HRSA",
    "USA",
    "US",
    "U.S.",
    "IT",
    "MD",
    "PA",
    "PC",
    "PBC",
    "LLC",
    "LLP",
    "PLLC",
    "OB/GYN",
    "LGBTQ",
    "HIV",
    "AIDS",
    "DBA",
    "SOC",
    "SIEM",
    "MDR",
    "XDR",
    "EDR",
    "DR",
    "MSP",
    "MSSP",
    "VCIO",
    "VCISO",
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.replace("\u2013", "-").replace("\u2014", "-").split())
    return str(value)


def _strip_legal_suffixes(value: str) -> str:
    current = value.strip()
    while True:
        updated = current
        for pattern in _LEGAL_SUFFIX_PATTERNS:
            updated = re.sub(pattern, "", updated, flags=re.IGNORECASE).strip(" ,.-")
        if updated == current:
            return updated
        current = updated


def _smart_title(text: str) -> str:
    if not text:
        return ""

    pieces = re.split(r"(\s+|[-/])", text)
    rendered: list[str] = []
    for piece in pieces:
        if not piece or piece.isspace() or piece in {"-", "/"}:
            rendered.append(piece)
            continue
        if piece.upper() in _UPPERCASE_KEEPERS:
            rendered.append(piece.upper())
            continue
        if any(character.isdigit() for character in piece):
            if "." in piece:
                head, separator, tail = piece.partition(".")
                if head and head[0].isalpha() and head == head.lower():
                    head = head[0].upper() + head[1:]
                rendered.append(head + separator + tail.lower())
                continue
            if any(character.isupper() for character in piece if character.isalpha()):
                rendered.append(piece)
                continue
            digit_prefix_match = re.fullmatch(r"(\d+)([a-z]+)", piece)
            if digit_prefix_match is not None:
                rendered.append(digit_prefix_match.group(1) + digit_prefix_match.group(2).capitalize())
                continue
            rendered.append(piece)
            continue
        if "&" in piece and any(character.isalpha() for character in piece):
            if any(character.isupper() for character in piece if character.isalpha()):
                rendered.append(piece)
            else:
                rendered.append("&".join(part[:1].upper() + part[1:].lower() for part in piece.split("&")))
            continue
        if re.fullmatch(r"[A-Z]{2,}", piece):
            rendered.append(piece)
            continue
        if "'" in piece:
            rendered.append("'".join(part[:1].upper() + part[1:].lower() for part in piece.split("'")))
            continue
        rendered.append(piece[:1].upper() + piece[1:].lower())
    return "".join(rendered)


def _split_dash_descriptor(value: str) -> str:
    parts = [part.strip(" ,-") for part in re.split(r"\s[-]\s", value) if part.strip(" ,-")]
    if len(parts) != 2:
        return value
    left, right = parts
    right_lower = right.lower()
    if any(hint in right_lower for hint in _DASH_DESCRIPTOR_HINTS):
        return left
    return value


def _collapse_parenthetical(value: str) -> str:
    # Keep short healthcare aliases like HOPE Clinic, but drop generic legal/status notes.
    return re.sub(r"\s+\((?:FQHC|CMHA|non-profit|nonprofit|501\(c\)\(3\)|dba.*?|doing business as.*?)\)$", "", value, flags=re.IGNORECASE).strip()


def _trim_descriptor_parenthetical(value: str) -> str:
    match = re.search(r"\s+\(([^)]{1,80})\)$", value)
    if match is None:
        return value
    descriptor = match.group(1).strip().casefold()
    if any(hint in descriptor for hint in _PARENTHETICAL_DESCRIPTOR_HINTS):
        return value[: match.start()].strip()
    return value


def _short_name(value: str) -> str:
    shortened = _trim_descriptor_parenthetical(value.strip())
    lowered = shortened.lower()
    for token in (
        "community health center",
        "community health centers",
        "health center",
        "health centers",
        "medical group",
        "family health center",
        "family health centers",
    ):
        if lowered.endswith(token):
            return shortened
    parts = shortened.split()
    if len(parts) >= 2 and parts[-1].strip(".,").casefold() in _GENERIC_TRAILING_TOKENS:
        shortened = " ".join(parts[:-1]).strip(" ,.-")
    return shortened


@dataclass(frozen=True, slots=True)
class CompanyNameNormalization:
    raw_name: str
    normalized_name: str
    short_name: str
    method: str


def heuristic_normalize_company_name(raw_name: str) -> CompanyNameNormalization:
    cleaned = _clean_text(raw_name).strip(" ,")
    if not cleaned:
        return CompanyNameNormalization(
            raw_name="",
            normalized_name="",
            short_name="",
            method="heuristic",
        )

    candidate = cleaned
    dba_match = re.search(r"\bdba\b(.+)$", candidate, flags=re.IGNORECASE)
    if dba_match is not None:
        candidate = dba_match.group(1).strip(" ,-")
    candidate = _split_dash_descriptor(candidate)
    candidate = _collapse_parenthetical(candidate)
    candidate = _trim_descriptor_parenthetical(candidate)
    candidate = _strip_legal_suffixes(candidate)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" ,.-")
    if (
        candidate.isupper()
        or candidate == candidate.lower()
        or any(character.isdigit() for character in candidate)
        or "&" in candidate
        or "." in candidate
    ):
        candidate = _smart_title(candidate)
    short = _short_name(candidate)
    return CompanyNameNormalization(
        raw_name=cleaned,
        normalized_name=candidate or cleaned,
        short_name=short or candidate or cleaned,
        method="heuristic",
    )


def needs_llm_company_name_cleanup(raw_name: str) -> bool:
    cleaned = _clean_text(raw_name)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    return any(
        token in lowered
        for token in (" dba ", " - ", " – ", " — ", " llc", " inc", " corporation", " company", " co.", "(", ")")
    )


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


class MiniMaxCompanyNameNormalizer:
    def __init__(self, client: MiniMaxClient) -> None:
        self._client = client

    async def normalize(
        self,
        *,
        raw_name: str,
        company_domain: str = "",
        company_description: str = "",
    ) -> CompanyNameNormalization:
        heuristic = heuristic_normalize_company_name(raw_name)
        response = await self._client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You normalize company names for outbound datasets. "
                        "Return only valid JSON. "
                        "Keep the canonical display name. "
                        "Remove legal suffixes like LLC or Inc unless they are essential to the brand. "
                        "Prefer the clean operating name over descriptors or recruiting appendages."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "Normalize a company display name.",
                            "required_output_schema": {
                                "normalized_name": "Clean display name with proper capitalization",
                                "short_name": "Shortened display name for copy if a shorter safe version exists; otherwise same as normalized_name",
                            },
                            "input": {
                                "raw_name": raw_name,
                                "company_domain": company_domain,
                                "company_description": company_description[:800],
                                "heuristic_guess": heuristic.normalized_name,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=200,
        )
        payload = response.data if isinstance(response.data, dict) else {}
        parsed = _extract_json_object(_message_content(payload)) or {}
        normalized_name = _clean_text(parsed.get("normalized_name")) or heuristic.normalized_name
        short_name = _clean_text(parsed.get("short_name")) or normalized_name
        return CompanyNameNormalization(
            raw_name=heuristic.raw_name,
            normalized_name=normalized_name,
            short_name=short_name,
            method="minimax",
        )
