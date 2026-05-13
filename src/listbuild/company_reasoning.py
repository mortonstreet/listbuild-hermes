from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from listbuild.providers import MiniMaxClient
from listbuild.research import CompanyResearchSummary, ResearchPage, summarize_company_research


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def _truncate(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit]


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


def _company_post_snippets(payload: dict[str, Any] | None, *, limit: int = 3) -> list[str]:
    if not isinstance(payload, dict):
        return []
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return []
    snippets: list[str] = []
    for item in elements[:limit]:
        if not isinstance(item, dict):
            continue
        content = _clean_text(item.get("content"))
        if content:
            snippets.append(_truncate(content, limit=320))
    return snippets


def _news_snippets(payload: dict[str, Any] | None, *, limit: int = 4) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("news")
    if not isinstance(items, list):
        return []
    news_rows: list[dict[str, str]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        news_rows.append(
            {
                "title": _clean_text(item.get("title")),
                "snippet": _truncate(_clean_text(item.get("snippet") or item.get("description")), limit=280),
                "link": _clean_text(item.get("link")),
            }
        )
    return news_rows


def build_company_evidence_bundle(
    *,
    company_payload: dict[str, Any] | None,
    company_posts_payload: dict[str, Any] | None,
    news_payload: dict[str, Any] | None,
    pages: list[ResearchPage],
) -> dict[str, Any]:
    element = company_payload.get("element") if isinstance(company_payload, dict) else {}
    if not isinstance(element, dict):
        element = {}
    return {
        "linkedin_company": {
            "name": _clean_text(element.get("name")),
            "website": _clean_text(element.get("website")),
            "description": _truncate(_clean_text(element.get("description") or element.get("tagline")), limit=1200),
            "employee_count": _clean_text(element.get("employeeCount")),
            "employee_count_range": _clean_text(element.get("employeeCountRange")),
            "follower_count": _clean_text(element.get("followerCount")),
            "job_search_url": _clean_text(element.get("jobSearchUrl")),
        },
        "linkedin_posts": _company_post_snippets(company_posts_payload),
        "news_results": _news_snippets(news_payload),
        "site_pages": [
            {
                "url": page.url,
                "title": _clean_text(page.title),
                "description": _truncate(_clean_text(page.description), limit=500),
                "markdown_excerpt": _truncate(_clean_text(page.markdown), limit=2500),
            }
            for page in pages
        ],
    }


@dataclass(frozen=True, slots=True)
class CompanyReasoningResult:
    summary: CompanyResearchSummary
    raw_response: dict[str, Any]
    parsed_response: dict[str, Any]


class MiniMaxCompanyReasoner:
    def __init__(self, client: MiniMaxClient) -> None:
        self._client = client

    async def summarize(
        self,
        *,
        seller: str,
        segment: str,
        seller_context: str,
        campaign_context: str,
        company_payload: dict[str, Any] | None,
        company_posts_payload: dict[str, Any] | None,
        news_payload: dict[str, Any] | None,
        pages: list[ResearchPage],
    ) -> CompanyReasoningResult:
        heuristic = summarize_company_research(
            company_payload=company_payload,
            company_posts_payload=company_posts_payload,
            news_payload=news_payload,
            pages=pages,
        )
        evidence = build_company_evidence_bundle(
            company_payload=company_payload,
            company_posts_payload=company_posts_payload,
            news_payload=news_payload,
            pages=pages,
        )
        response = await self._client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a deterministic B2B research analyst. "
                        "Return only valid JSON. "
                        "Do not invent facts. "
                        "If evidence is weak, use empty strings instead of guessing."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "Summarize company-level research for outbound personalization.",
                            "seller": seller,
                            "segment": segment,
                            "seller_context": seller_context,
                            "campaign_context": campaign_context,
                            "required_output_schema": {
                                "company_description": "1-3 sentence factual company description",
                                "company_offer": "what the company sells or delivers",
                                "company_icp": "who the company appears to sell to",
                                "company_painpoint": "likely business bottleneck or operating pain relevant to the seller context",
                                "company_signals": "short signal summary, pipe-separated if multiple",
                                "company_signal_sources": "source urls or source labels, pipe-separated if multiple",
                            },
                            "evidence": evidence,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        payload = response.data if isinstance(response.data, dict) else {}
        content = _message_content(payload)
        parsed = _extract_json_object(content) or {}
        summary = CompanyResearchSummary(
            description=_clean_text(parsed.get("company_description")) or heuristic.description,
            offer=_clean_text(parsed.get("company_offer")) or heuristic.offer,
            icp=_clean_text(parsed.get("company_icp")) or heuristic.icp,
            painpoint=_clean_text(parsed.get("company_painpoint")) or heuristic.painpoint,
            signals=_clean_text(parsed.get("company_signals")) or heuristic.signals,
            signal_sources=_clean_text(parsed.get("company_signal_sources")) or heuristic.signal_sources,
        )
        return CompanyReasoningResult(
            summary=summary,
            raw_response=payload,
            parsed_response=parsed,
        )
