from __future__ import annotations

from dataclasses import dataclass
import math


def _safe_divide(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class CoveragePlan:
    target_valid_emails: int
    observed_companies: int
    observed_people: int
    observed_valid_emails: int
    email_coverage_rate: float
    people_per_company: float
    emails_per_company: float
    emails_per_person: float
    company_multiplier_from_v1: float
    recommended_total_companies: int
    recommended_additional_companies: int
    recommended_total_people: int
    buffer_multiplier: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "target_valid_emails": self.target_valid_emails,
            "observed_companies": self.observed_companies,
            "observed_people": self.observed_people,
            "observed_valid_emails": self.observed_valid_emails,
            "email_coverage_rate": round(self.email_coverage_rate, 6),
            "people_per_company": round(self.people_per_company, 6),
            "emails_per_company": round(self.emails_per_company, 6),
            "emails_per_person": round(self.emails_per_person, 6),
            "company_multiplier_from_v1": round(self.company_multiplier_from_v1, 6),
            "recommended_total_companies": self.recommended_total_companies,
            "recommended_additional_companies": self.recommended_additional_companies,
            "recommended_total_people": self.recommended_total_people,
            "buffer_multiplier": round(self.buffer_multiplier, 6),
        }


def build_coverage_plan(
    *,
    target_valid_emails: int,
    observed_companies: int,
    observed_people: int,
    observed_valid_emails: int,
    buffer_multiplier: float = 1.15,
) -> CoveragePlan:
    if target_valid_emails <= 0:
        raise ValueError("target_valid_emails must be greater than zero")
    if observed_companies <= 0:
        raise ValueError("observed_companies must be greater than zero")
    if observed_people < 0 or observed_valid_emails < 0:
        raise ValueError("observed_people and observed_valid_emails cannot be negative")

    email_coverage_rate = _safe_divide(observed_valid_emails, observed_people)
    people_per_company = _safe_divide(observed_people, observed_companies)
    emails_per_company = _safe_divide(observed_valid_emails, observed_companies)
    emails_per_person = _safe_divide(observed_valid_emails, observed_people)

    if emails_per_company <= 0:
        recommended_total_companies = 0
        recommended_total_people = 0
        company_multiplier = 0.0
    else:
        recommended_total_companies = math.ceil(
            (target_valid_emails / emails_per_company) * buffer_multiplier
        )
        recommended_total_people = math.ceil(
            recommended_total_companies * people_per_company
        )
        company_multiplier = _safe_divide(
            recommended_total_companies,
            observed_companies,
        )

    recommended_additional_companies = max(
        0,
        recommended_total_companies - observed_companies,
    )

    return CoveragePlan(
        target_valid_emails=target_valid_emails,
        observed_companies=observed_companies,
        observed_people=observed_people,
        observed_valid_emails=observed_valid_emails,
        email_coverage_rate=email_coverage_rate,
        people_per_company=people_per_company,
        emails_per_company=emails_per_company,
        emails_per_person=emails_per_person,
        company_multiplier_from_v1=company_multiplier,
        recommended_total_companies=recommended_total_companies,
        recommended_additional_companies=recommended_additional_companies,
        recommended_total_people=recommended_total_people,
        buffer_multiplier=buffer_multiplier,
    )
