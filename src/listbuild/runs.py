from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    return cleaned.strip("-")


def current_run_date() -> str:
    return datetime.now().strftime("%m%d%Y")


@dataclass(frozen=True, slots=True)
class RunNaming:
    seller_slug: str
    segment_slug: str
    date_stamp: str

    @property
    def run_slug(self) -> str:
        return f"{self.seller_slug}-{self.segment_slug}-{self.date_stamp}"

    @property
    def company_csv_name(self) -> str:
        return f"{self.run_slug}-companies.csv"

    @property
    def people_csv_name(self) -> str:
        return f"{self.run_slug}-people.csv"

    @property
    def company_jsonl_name(self) -> str:
        return f"{self.run_slug}-harvest-company.jsonl"

    @property
    def profile_jsonl_name(self) -> str:
        return f"{self.run_slug}-harvest-profile.jsonl"

    @property
    def serper_jsonl_name(self) -> str:
        return f"{self.run_slug}-serper-search.jsonl"

    @property
    def firecrawl_jsonl_name(self) -> str:
        return f"{self.run_slug}-firecrawl.jsonl"

    @property
    def budget_jsonl_name(self) -> str:
        return f"{self.run_slug}-budget-events.jsonl"

    @property
    def budget_summary_name(self) -> str:
        return f"{self.run_slug}-budget-summary.json"


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path
    naming: RunNaming

    @property
    def run_dir(self) -> Path:
        return self.root / self.naming.run_slug

    @property
    def raw_dir(self) -> Path:
        return self.run_dir / "raw"

    @property
    def output_dir(self) -> Path:
        return self.run_dir / "outputs"

    @property
    def company_csv(self) -> Path:
        return self.output_dir / self.naming.company_csv_name

    @property
    def people_csv(self) -> Path:
        return self.output_dir / self.naming.people_csv_name

    @property
    def harvest_company_jsonl(self) -> Path:
        return self.raw_dir / self.naming.company_jsonl_name

    @property
    def harvest_profile_jsonl(self) -> Path:
        return self.raw_dir / self.naming.profile_jsonl_name

    @property
    def serper_jsonl(self) -> Path:
        return self.raw_dir / self.naming.serper_jsonl_name

    @property
    def firecrawl_jsonl(self) -> Path:
        return self.raw_dir / self.naming.firecrawl_jsonl_name

    @property
    def budget_jsonl(self) -> Path:
        return self.raw_dir / self.naming.budget_jsonl_name

    @property
    def budget_summary(self) -> Path:
        return self.output_dir / self.naming.budget_summary_name

    def to_dict(self) -> dict[str, str]:
        return {
            "run_slug": self.naming.run_slug,
            "run_dir": str(self.run_dir),
            "raw_dir": str(self.raw_dir),
            "output_dir": str(self.output_dir),
            "company_csv": str(self.company_csv),
            "people_csv": str(self.people_csv),
            "harvest_company_jsonl": str(self.harvest_company_jsonl),
            "harvest_profile_jsonl": str(self.harvest_profile_jsonl),
            "serper_jsonl": str(self.serper_jsonl),
            "firecrawl_jsonl": str(self.firecrawl_jsonl),
            "budget_jsonl": str(self.budget_jsonl),
            "budget_summary": str(self.budget_summary),
        }


def build_run_paths(
    *,
    seller: str,
    segment: str,
    date_stamp: str | None = None,
    root: str | Path = "runs",
) -> RunPaths:
    naming = RunNaming(
        seller_slug=slugify(seller),
        segment_slug=slugify(segment),
        date_stamp=date_stamp or current_run_date(),
    )
    return RunPaths(root=Path(root), naming=naming)


def scaffold_run_directories(paths: RunPaths) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    paths.output_dir.mkdir(parents=True, exist_ok=True)
