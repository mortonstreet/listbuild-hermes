from __future__ import annotations

from dataclasses import dataclass

from .models import PipelineTemplate


@dataclass(frozen=True, slots=True)
class BlueprintStage:
    stage: str
    input_contract: list[str]
    runtime_decisions: list[str]
    output_contract: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "input_contract": self.input_contract,
            "runtime_decisions": self.runtime_decisions,
            "output_contract": self.output_contract,
        }


def build_pipeline_blueprint(template: PipelineTemplate) -> list[BlueprintStage]:
    role_labels = [
        role.segment_name
        for role in sorted(template.roles, key=lambda item: item.priority)
    ]
    return [
        BlueprintStage(
            stage="0_offer_definition",
            input_contract=[
                template.seller.offer_name,
                template.seller.seller_pov,
                template.buyer.segment_name,
                template.buyer.company_query,
            ],
            runtime_decisions=[
                "Lock seller POV before any search so the run optimizes for buyer pain, not generic enrichment.",
                "Turn the offer into signal buckets and an explicit scoring goal.",
            ],
            output_contract=[
                "normalized offer brief",
                "buyer pain hypotheses",
                "scoring rubric seed",
            ],
        ),
        BlueprintStage(
            stage="1_universe_discovery",
            input_contract=[
                template.buyer.company_query,
                *template.buyer.company_filters,
            ],
            runtime_decisions=[
                "Use Serper as the recall layer to discover candidate firms.",
                "Dedupe on normalized domain and LinkedIn company identity.",
                "Stop when the accepted company count reaches the run target.",
            ],
            output_contract=[
                "company candidates with search lineage",
                "LinkedIn company URLs",
                "normalized company names",
            ],
        ),
        BlueprintStage(
            stage="2_company_identity_and_org_map",
            input_contract=[
                "company candidate",
                "target role segments",
                *role_labels,
            ],
            runtime_decisions=[
                "Resolve company details with Harvest first because it validates LinkedIn identity and may return the domain.",
                "If domain is missing, fall back to a deterministic Serper official-site lookup.",
                "Use role-segmented Serper searches to recover the visible org and candidate LinkedIn profile URLs.",
            ],
            output_contract=[
                "company domain",
                "org map by role segment",
                "candidate people with LinkedIn URLs",
            ],
        ),
        BlueprintStage(
            stage="3_people_validation_and_enrichment",
            input_contract=[
                "candidate person",
                "LinkedIn profile URL",
                "company domain",
            ],
            runtime_decisions=[
                "Pull Harvest profile JSON to validate role, company, tenure, and activity.",
                "Escalate to Prospeo only when Harvest or search recall misses coverage.",
                "Verify emails after recovery, not during every upstream search step.",
            ],
            output_contract=[
                "validated people",
                "role and seniority evidence",
                "person-level signal stack",
            ],
        ),
        BlueprintStage(
            stage="4_company_war_chest",
            input_contract=[
                "company domain",
                "company LinkedIn identity",
            ],
            runtime_decisions=[
                "Run a domain-level Firecrawl crawl to capture product, focus, portfolio, PR, and specialization signals.",
                "Extract first-party thesis signals before branching into wider web research.",
            ],
            output_contract=[
                "site-level content corpus",
                "first-party company summary",
                "specialization hypotheses",
            ],
        ),
        BlueprintStage(
            stage="5_signal_branching",
            input_contract=[
                "company specialization hypotheses",
                *template.research.macro_signal_buckets,
            ],
            runtime_decisions=[
                "Spawn follow-up Serper searches from detected sectors, portfolio concentration, and buyer pain clues.",
                "Scrape only the strongest corroborating sources instead of crawling the whole web.",
                "Accumulate a context war chest that supports a domain-expert point of view.",
            ],
            output_contract=[
                "macro context",
                "market pressure signals",
                "pain or bottleneck evidence",
            ],
        ),
        BlueprintStage(
            stage="6_scoring_and_export",
            input_contract=[
                "validated company context",
                "validated people context",
                "macro context",
            ],
            runtime_decisions=[
                "Score against the explicit rubric, not against vague relevance.",
                "Weight pain intensity and solvability ahead of generic fit.",
                "Keep role-level personalization inputs separate from account-level scoring outputs.",
            ],
            output_contract=[
                "account score",
                "contact score",
                "structured research pack for downstream copy generation",
            ],
        ),
    ]
