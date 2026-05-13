from __future__ import annotations

from dataclasses import dataclass

from .models import CostAssumptions, PipelineTemplate, UnitCosts


def _round_money(value: float) -> float:
    return round(value, 4)


@dataclass(frozen=True, slots=True)
class CostLineItem:
    stage: str
    operation: str
    volume: float
    unit_cost_usd: float
    total_cost_usd: float

    def to_dict(self) -> dict[str, float | str]:
        return {
            "stage": self.stage,
            "operation": self.operation,
            "volume": round(self.volume, 4),
            "unit_cost_usd": _round_money(self.unit_cost_usd),
            "total_cost_usd": _round_money(self.total_cost_usd),
        }


@dataclass(frozen=True, slots=True)
class PipelineCostEstimate:
    template_name: str
    target_company_count: int
    estimated_people_count: float
    line_items: list[CostLineItem]
    total_cost_usd: float
    cost_per_company_usd: float
    cost_per_person_usd: float

    def to_dict(self) -> dict[str, object]:
        return {
            "template_name": self.template_name,
            "target_company_count": self.target_company_count,
            "estimated_people_count": round(self.estimated_people_count, 4),
            "total_cost_usd": _round_money(self.total_cost_usd),
            "cost_per_company_usd": _round_money(self.cost_per_company_usd),
            "cost_per_person_usd": _round_money(self.cost_per_person_usd),
            "line_items": [item.to_dict() for item in self.line_items],
        }


def estimate_pipeline_cost(template: PipelineTemplate) -> PipelineCostEstimate:
    assumptions = template.cost_model.assumptions
    unit_costs = template.cost_model.unit_costs
    target_companies = assumptions.target_company_count
    estimated_people = target_companies * assumptions.average_people_per_company
    line_items = [
        _serper_universe_discovery(assumptions, unit_costs),
        _harvest_company_resolution(assumptions, unit_costs),
        _serper_domain_fallbacks(assumptions, unit_costs),
        _serper_org_discovery(assumptions, unit_costs),
        _harvest_people_search(assumptions, unit_costs),
        _harvest_profile_enrichment(assumptions, unit_costs),
        _prospeo_person_fallback(assumptions, unit_costs),
        _firecrawl_company_crawl(assumptions, unit_costs),
        _serper_followup_research(assumptions, unit_costs),
        _firecrawl_followup_research(assumptions, unit_costs),
        _million_verifier_validation(assumptions, unit_costs),
        _llm_scoring_and_analysis(assumptions, unit_costs),
    ]
    total_cost = sum(item.total_cost_usd for item in line_items)
    return PipelineCostEstimate(
        template_name=template.template_name,
        target_company_count=target_companies,
        estimated_people_count=estimated_people,
        line_items=line_items,
        total_cost_usd=total_cost,
        cost_per_company_usd=(
            total_cost / target_companies if target_companies else 0.0
        ),
        cost_per_person_usd=(total_cost / estimated_people if estimated_people else 0.0),
    )


def _item(stage: str, operation: str, volume: float, unit_cost_usd: float) -> CostLineItem:
    return CostLineItem(
        stage=stage,
        operation=operation,
        volume=volume,
        unit_cost_usd=unit_cost_usd,
        total_cost_usd=volume * unit_cost_usd,
    )


def _serper_universe_discovery(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = assumptions.seed_serper_queries * assumptions.serper_pages_per_seed_query
    return _item("universe_discovery", "serper_search", volume, unit_costs.serper_search_usd)


def _harvest_company_resolution(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = assumptions.target_company_count * assumptions.harvest_company_calls_per_company
    return _item(
        "company_resolution",
        "harvest_company_call",
        volume,
        unit_costs.harvest_company_call_usd,
    )


def _serper_domain_fallbacks(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = (
        assumptions.target_company_count
        * assumptions.domain_fallback_rate
        * assumptions.fallback_serper_queries_per_company
    )
    return _item(
        "company_resolution",
        "serper_domain_fallback",
        volume,
        unit_costs.serper_search_usd,
    )


def _serper_org_discovery(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = (
        assumptions.target_company_count
        * assumptions.role_segments_per_company
        * assumptions.serper_people_queries_per_role_segment
    )
    return _item(
        "org_discovery",
        "serper_people_search",
        volume,
        unit_costs.serper_search_usd,
    )


def _harvest_people_search(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = assumptions.target_company_count * assumptions.harvest_people_search_calls_per_company
    return _item(
        "org_validation",
        "harvest_people_search",
        volume,
        unit_costs.harvest_people_search_call_usd,
    )


def _harvest_profile_enrichment(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = (
        assumptions.target_company_count
        * assumptions.average_people_per_company
        * assumptions.harvest_profile_calls_per_person
    )
    return _item(
        "people_enrichment",
        "harvest_profile_get",
        volume,
        unit_costs.harvest_profile_call_usd,
    )


def _prospeo_person_fallback(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    people = assumptions.target_company_count * assumptions.average_people_per_company
    search_pages = assumptions.target_company_count * assumptions.prospeo_people_search_pages_per_company
    enriches = people * assumptions.prospeo_enrich_person_rate
    total_cost = (
        search_pages * unit_costs.prospeo_search_page_usd
        + enriches * unit_costs.prospeo_enrich_person_usd
    )
    return CostLineItem(
        stage="fallback_enrichment",
        operation="prospeo_search_and_enrich",
        volume=search_pages + enriches,
        unit_cost_usd=0.0,
        total_cost_usd=total_cost,
    )


def _firecrawl_company_crawl(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = assumptions.target_company_count * assumptions.firecrawl_crawl_pages_per_company
    unit_cost = unit_costs.firecrawl_credit_usd * unit_costs.firecrawl_crawl_credits_per_page
    return _item("company_war_chest", "firecrawl_crawl_pages", volume, unit_cost)


def _serper_followup_research(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = (
        assumptions.target_company_count * assumptions.serper_followup_queries_per_company
        + assumptions.target_company_count
        * assumptions.macro_branches_per_company
        * assumptions.serper_queries_per_macro_branch
    )
    return _item(
        "followup_research",
        "serper_followup_search",
        volume,
        unit_costs.serper_search_usd,
    )


def _firecrawl_followup_research(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = (
        assumptions.target_company_count
        * assumptions.firecrawl_followup_scrape_pages_per_company
        + assumptions.target_company_count
        * assumptions.macro_branches_per_company
        * assumptions.firecrawl_pages_per_macro_branch
    )
    unit_cost = unit_costs.firecrawl_credit_usd * unit_costs.firecrawl_scrape_credits_per_page
    return _item(
        "followup_research",
        "firecrawl_followup_pages",
        volume,
        unit_cost,
    )


def _million_verifier_validation(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = (
        assumptions.target_company_count
        * assumptions.average_people_per_company
        * assumptions.verification_rate_per_person
    )
    return _item(
        "verification",
        "million_verifier_email",
        volume,
        unit_costs.million_verifier_email_usd,
    )


def _llm_scoring_and_analysis(
    assumptions: CostAssumptions,
    unit_costs: UnitCosts,
) -> CostLineItem:
    volume = assumptions.target_company_count
    return _item(
        "scoring",
        "llm_analysis",
        volume,
        unit_costs.llm_analysis_per_company_usd,
    )
