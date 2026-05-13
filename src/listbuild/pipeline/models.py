from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


def _require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required string field: {key}")
    return value.strip()


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"Expected {key} to be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"Expected every item in {key} to be a string")
        if item.strip():
            result.append(item.strip())
    return result


def _float_value(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Expected {key} to be numeric")


def _int_value(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"Expected {key} to be an integer")


@dataclass(frozen=True, slots=True)
class SellerOffer:
    seller_name: str
    offer_name: str
    seller_pov: str
    outcome_hypothesis: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SellerOffer":
        return cls(
            seller_name=_require_string(data, "seller_name"),
            offer_name=_require_string(data, "offer_name"),
            seller_pov=_require_string(data, "seller_pov"),
            outcome_hypothesis=_require_string(data, "outcome_hypothesis"),
        )


@dataclass(frozen=True, slots=True)
class BuyerSegment:
    segment_name: str
    company_query: str
    company_filters: list[str]
    pain_hypotheses: list[str]
    scoring_goal: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BuyerSegment":
        return cls(
            segment_name=_require_string(data, "segment_name"),
            company_query=_require_string(data, "company_query"),
            company_filters=_string_list(data, "company_filters"),
            pain_hypotheses=_string_list(data, "pain_hypotheses"),
            scoring_goal=_require_string(data, "scoring_goal"),
        )


@dataclass(frozen=True, slots=True)
class RoleSegment:
    segment_name: str
    target_titles: list[str]
    purpose: str
    priority: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoleSegment":
        return cls(
            segment_name=_require_string(data, "segment_name"),
            target_titles=_string_list(data, "target_titles"),
            purpose=_require_string(data, "purpose"),
            priority=_int_value(data, "priority", 0),
        )


@dataclass(frozen=True, slots=True)
class ScoringFactor:
    name: str
    weight: float
    description: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoringFactor":
        return cls(
            name=_require_string(data, "name"),
            weight=_float_value(data, "weight"),
            description=_require_string(data, "description"),
        )


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    company_signal_buckets: list[str]
    person_signal_buckets: list[str]
    macro_signal_buckets: list[str]
    scoring_factors: list[ScoringFactor]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchPlan":
        scoring = data.get("scoring_factors", [])
        if not isinstance(scoring, list):
            raise ValueError("Expected scoring_factors to be a list")
        return cls(
            company_signal_buckets=_string_list(data, "company_signal_buckets"),
            person_signal_buckets=_string_list(data, "person_signal_buckets"),
            macro_signal_buckets=_string_list(data, "macro_signal_buckets"),
            scoring_factors=[
                ScoringFactor.from_dict(item)
                for item in scoring
                if isinstance(item, dict)
            ],
        )


@dataclass(frozen=True, slots=True)
class UnitCosts:
    serper_search_usd: float
    firecrawl_credit_usd: float
    firecrawl_scrape_credits_per_page: float
    firecrawl_crawl_credits_per_page: float
    firecrawl_search_credits_per_result: float
    harvest_company_call_usd: float
    harvest_people_search_call_usd: float
    harvest_profile_call_usd: float
    prospeo_search_page_usd: float
    prospeo_enrich_company_usd: float
    prospeo_enrich_person_usd: float
    million_verifier_email_usd: float
    llm_analysis_per_company_usd: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnitCosts":
        return cls(
            serper_search_usd=_float_value(data, "serper_search_usd"),
            firecrawl_credit_usd=_float_value(data, "firecrawl_credit_usd"),
            firecrawl_scrape_credits_per_page=_float_value(
                data,
                "firecrawl_scrape_credits_per_page",
                1.0,
            ),
            firecrawl_crawl_credits_per_page=_float_value(
                data,
                "firecrawl_crawl_credits_per_page",
                1.0,
            ),
            firecrawl_search_credits_per_result=_float_value(
                data,
                "firecrawl_search_credits_per_result",
                1.0,
            ),
            harvest_company_call_usd=_float_value(data, "harvest_company_call_usd"),
            harvest_people_search_call_usd=_float_value(
                data,
                "harvest_people_search_call_usd",
            ),
            harvest_profile_call_usd=_float_value(data, "harvest_profile_call_usd"),
            prospeo_search_page_usd=_float_value(data, "prospeo_search_page_usd"),
            prospeo_enrich_company_usd=_float_value(
                data,
                "prospeo_enrich_company_usd",
            ),
            prospeo_enrich_person_usd=_float_value(
                data,
                "prospeo_enrich_person_usd",
            ),
            million_verifier_email_usd=_float_value(
                data,
                "million_verifier_email_usd",
            ),
            llm_analysis_per_company_usd=_float_value(
                data,
                "llm_analysis_per_company_usd",
            ),
        )


@dataclass(frozen=True, slots=True)
class CostAssumptions:
    target_company_count: int
    seed_serper_queries: int
    serper_pages_per_seed_query: int
    harvest_company_calls_per_company: float
    domain_fallback_rate: float
    fallback_serper_queries_per_company: float
    role_segments_per_company: float
    serper_people_queries_per_role_segment: float
    average_people_per_company: float
    harvest_people_search_calls_per_company: float
    harvest_profile_calls_per_person: float
    prospeo_people_search_pages_per_company: float
    prospeo_enrich_person_rate: float
    firecrawl_crawl_pages_per_company: float
    firecrawl_followup_scrape_pages_per_company: float
    serper_followup_queries_per_company: float
    macro_branches_per_company: float
    serper_queries_per_macro_branch: float
    firecrawl_pages_per_macro_branch: float
    verification_rate_per_person: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CostAssumptions":
        return cls(
            target_company_count=_int_value(data, "target_company_count"),
            seed_serper_queries=_int_value(data, "seed_serper_queries"),
            serper_pages_per_seed_query=_int_value(data, "serper_pages_per_seed_query", 1),
            harvest_company_calls_per_company=_float_value(
                data,
                "harvest_company_calls_per_company",
            ),
            domain_fallback_rate=_float_value(data, "domain_fallback_rate"),
            fallback_serper_queries_per_company=_float_value(
                data,
                "fallback_serper_queries_per_company",
            ),
            role_segments_per_company=_float_value(data, "role_segments_per_company"),
            serper_people_queries_per_role_segment=_float_value(
                data,
                "serper_people_queries_per_role_segment",
            ),
            average_people_per_company=_float_value(data, "average_people_per_company"),
            harvest_people_search_calls_per_company=_float_value(
                data,
                "harvest_people_search_calls_per_company",
            ),
            harvest_profile_calls_per_person=_float_value(
                data,
                "harvest_profile_calls_per_person",
            ),
            prospeo_people_search_pages_per_company=_float_value(
                data,
                "prospeo_people_search_pages_per_company",
            ),
            prospeo_enrich_person_rate=_float_value(
                data,
                "prospeo_enrich_person_rate",
            ),
            firecrawl_crawl_pages_per_company=_float_value(
                data,
                "firecrawl_crawl_pages_per_company",
            ),
            firecrawl_followup_scrape_pages_per_company=_float_value(
                data,
                "firecrawl_followup_scrape_pages_per_company",
            ),
            serper_followup_queries_per_company=_float_value(
                data,
                "serper_followup_queries_per_company",
            ),
            macro_branches_per_company=_float_value(
                data,
                "macro_branches_per_company",
            ),
            serper_queries_per_macro_branch=_float_value(
                data,
                "serper_queries_per_macro_branch",
            ),
            firecrawl_pages_per_macro_branch=_float_value(
                data,
                "firecrawl_pages_per_macro_branch",
            ),
            verification_rate_per_person=_float_value(
                data,
                "verification_rate_per_person",
            ),
        )


@dataclass(frozen=True, slots=True)
class CostModel:
    unit_costs: UnitCosts
    assumptions: CostAssumptions

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CostModel":
        unit_costs = data.get("unit_costs", {})
        assumptions = data.get("assumptions", {})
        if not isinstance(unit_costs, dict) or not isinstance(assumptions, dict):
            raise ValueError("cost_model.unit_costs and cost_model.assumptions must be objects")
        return cls(
            unit_costs=UnitCosts.from_dict(unit_costs),
            assumptions=CostAssumptions.from_dict(assumptions),
        )


@dataclass(frozen=True, slots=True)
class PipelineTemplate:
    template_name: str
    seller: SellerOffer
    buyer: BuyerSegment
    roles: list[RoleSegment]
    research: ResearchPlan
    cost_model: CostModel

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineTemplate":
        seller = data.get("seller", {})
        buyer = data.get("buyer", {})
        roles = data.get("roles", [])
        research = data.get("research", {})
        cost_model = data.get("cost_model", {})
        if not isinstance(seller, dict) or not isinstance(buyer, dict):
            raise ValueError("seller and buyer must be objects")
        if not isinstance(roles, list):
            raise ValueError("roles must be a list")
        if not isinstance(research, dict) or not isinstance(cost_model, dict):
            raise ValueError("research and cost_model must be objects")
        return cls(
            template_name=_require_string(data, "template_name"),
            seller=SellerOffer.from_dict(seller),
            buyer=BuyerSegment.from_dict(buyer),
            roles=[
                RoleSegment.from_dict(item)
                for item in roles
                if isinstance(item, dict)
            ],
            research=ResearchPlan.from_dict(research),
            cost_model=CostModel.from_dict(cost_model),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_pipeline_template(path: str | Path) -> PipelineTemplate:
    target = Path(path)
    return PipelineTemplate.from_dict(
        json.loads(target.read_text(encoding="utf-8"))
    )
