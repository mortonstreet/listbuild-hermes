from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse

from listbuild.budget import RunBudgetTracker, ScrapePricing
from listbuild.company_reasoning import MiniMaxCompanyReasoner
from listbuild.datasets import CompanyRow, company_quality_score, write_company_rows_csv
from listbuild.jsonl import append_jsonl
from listbuild.providers import (
    BrightDataClient,
    FirecrawlClient,
    HarvestClient,
    MiniMaxClient,
    SerperClient,
)
from listbuild.research import (
    ResearchPage,
    brightdata_discovery_seed_urls,
    guess_research_urls,
    summarize_company_posts,
    summarize_company_research,
    select_brightdata_research_urls,
    select_research_urls,
)
from listbuild.runs import RunPaths, build_run_paths, scaffold_run_directories


@dataclass(frozen=True, slots=True)
class CompanyResearchPolicy:
    people_csv: str | None = None
    companies_csv: str | None = None
    corpus_companies_csv: str | None = None
    qualifier_only: bool = True
    max_companies: int = 0
    max_research_pages: int = 6
    include_company_posts: bool = True
    posted_limit: str = "month"
    research_page_timeout_ms: int = 60000
    use_minimax: bool = True
    crawler_provider: str = "firecrawl"
    brightdata_domain_discovery: bool = True
    seller_context: str = ""
    campaign_context: str = ""


@dataclass(frozen=True, slots=True)
class ResearchCompanyTarget:
    company_slug: str
    company_name: str
    company_domain: str
    company_linkedin_url: str
    source_query: str


@dataclass(slots=True)
class CompanyResearchRun:
    run_slug: str
    company_count: int
    company_csv: str
    people_context_csv: str | None
    raw_harvest_company_jsonl: str
    raw_firecrawl_jsonl: str
    raw_serper_jsonl: str
    raw_minimax_jsonl: str | None
    budget: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "company_count": self.company_count,
            "company_csv": self.company_csv,
            "people_context_csv": self.people_context_csv,
            "raw_harvest_company_jsonl": self.raw_harvest_company_jsonl,
            "raw_firecrawl_jsonl": self.raw_firecrawl_jsonl,
            "raw_serper_jsonl": self.raw_serper_jsonl,
            "raw_minimax_jsonl": self.raw_minimax_jsonl,
            "budget": self.budget,
        }


class CompanyResearchWorkflow:
    def __init__(
        self,
        serper: SerperClient,
        harvest: HarvestClient,
        firecrawl: FirecrawlClient | None = None,
        brightdata: BrightDataClient | None = None,
        minimax: MiniMaxClient | None = None,
    ) -> None:
        self._serper = serper
        self._harvest = harvest
        self._firecrawl = firecrawl
        self._brightdata = brightdata
        self._minimax = minimax

    async def run(
        self,
        *,
        seller: str,
        segment: str,
        date_stamp: str | None = None,
        root: str = "runs",
        policy: CompanyResearchPolicy,
        budget_tracker: RunBudgetTracker | None = None,
        pricing: ScrapePricing | None = None,
    ) -> CompanyResearchRun:
        if not policy.people_csv and not policy.companies_csv:
            raise ValueError("One of people_csv or companies_csv is required")
        if policy.use_minimax and self._minimax is None:
            raise ValueError("MiniMax is not configured. Add MINIMAX_API_KEY or disable --use-minimax.")
        if policy.crawler_provider == "firecrawl" and self._firecrawl is None:
            raise ValueError("Firecrawl is not configured for this workflow run.")
        if policy.crawler_provider == "brightdata" and self._brightdata is None:
            raise ValueError("Bright Data is not configured. Add BRIGHTDATA_API_KEY and BRIGHTDATA_CRAWL_DATASET_ID.")

        paths = build_run_paths(
            seller=seller,
            segment=segment,
            date_stamp=date_stamp,
            root=root,
        )
        scaffold_run_directories(paths)
        tracker = budget_tracker or RunBudgetTracker(
            run_slug=paths.naming.run_slug,
            events_path=paths.budget_jsonl,
            summary_path=paths.budget_summary,
            pricing=pricing,
        )
        llm_jsonl_path = paths.raw_dir / f"{paths.naming.run_slug}-minimax-company-reasoning.jsonl"

        company_lookup = self._load_corpus_company_lookup(policy.corpus_companies_csv)
        original_people_rows = self._read_csv(policy.people_csv) if policy.people_csv else []
        targets = self._load_company_targets(
            people_rows=original_people_rows,
            companies_rows=self._read_csv(policy.companies_csv) if policy.companies_csv else [],
            company_lookup=company_lookup,
            qualifier_only=policy.qualifier_only,
            max_companies=policy.max_companies,
        )

        reasoner = (
            MiniMaxCompanyReasoner(self._minimax)
            if policy.use_minimax and self._minimax is not None
            else None
        )
        seller_context = policy.seller_context.strip() or seller
        campaign_context = policy.campaign_context.strip() or self._default_campaign_context(
            seller=seller,
            segment=segment,
        )

        companies = await self._research_companies(
            paths=paths,
            targets=targets,
            policy=policy,
            seller=seller,
            segment=segment,
            seller_context=seller_context,
            campaign_context=campaign_context,
            budget_tracker=tracker,
            reasoner=reasoner,
            llm_jsonl_path=llm_jsonl_path,
        )
        write_company_rows_csv(companies, paths.company_csv)

        people_context_csv: str | None = None
        if original_people_rows:
            people_context_path = paths.output_dir / f"{paths.naming.run_slug}-people-with-company-context.csv"
            self._write_people_context_csv(
                input_rows=original_people_rows,
                researched_companies=companies,
                output_path=people_context_path,
            )
            people_context_csv = str(people_context_path)

        budget_summary = tracker.write_summary()
        return CompanyResearchRun(
            run_slug=paths.naming.run_slug,
            company_count=len(companies),
            company_csv=str(paths.company_csv),
            people_context_csv=people_context_csv,
            raw_harvest_company_jsonl=str(paths.harvest_company_jsonl),
            raw_firecrawl_jsonl=str(paths.firecrawl_jsonl),
            raw_serper_jsonl=str(paths.serper_jsonl),
            raw_minimax_jsonl=str(llm_jsonl_path) if reasoner is not None else None,
            budget=budget_summary,
        )

    @staticmethod
    def _read_csv(path: str | None) -> list[dict[str, str]]:
        if path is None:
            return []
        csv.field_size_limit(sys.maxsize)
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def _is_truthy(value: str) -> bool:
        return (value or "").strip().lower() in {"true", "yes", "1", "y"}

    @staticmethod
    def _load_corpus_company_lookup(path: str | None) -> dict[str, dict[str, str]]:
        if path is None:
            return {}
        rows: dict[str, dict[str, str]] = {}
        csv.field_size_limit(sys.maxsize)
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                slug = (row.get("linkedin_slug") or "").strip()
                if slug:
                    rows[slug] = row
        return rows

    def _load_company_targets(
        self,
        *,
        people_rows: list[dict[str, str]],
        companies_rows: list[dict[str, str]],
        company_lookup: dict[str, dict[str, str]],
        qualifier_only: bool,
        max_companies: int,
    ) -> list[ResearchCompanyTarget]:
        targets_by_key: dict[str, ResearchCompanyTarget] = {}

        def register(
            *,
            company_slug: str,
            company_name: str,
            company_domain: str,
            company_linkedin_url: str,
            source_query: str,
        ) -> None:
            lookup_row = company_lookup.get(company_slug or "", {})
            linkedin_url = company_linkedin_url or (lookup_row.get("linkedin_url") or "").strip()
            domain = company_domain or self._parse_domain(lookup_row.get("resolved_domain") or "")
            name = company_name or (lookup_row.get("company_name") or "").strip()
            key = company_slug or linkedin_url or domain or name
            if not key:
                return
            targets_by_key[key] = ResearchCompanyTarget(
                company_slug=company_slug,
                company_name=name,
                company_domain=domain,
                company_linkedin_url=linkedin_url,
                source_query=source_query,
            )

        if people_rows:
            for row in people_rows:
                if qualifier_only and "person_soft_qualifies" in row:
                    if not self._is_truthy(row.get("person_soft_qualifies") or ""):
                        continue
                register(
                    company_slug=(row.get("source_company_slug") or row.get("company_key") or "").strip(),
                    company_name=(row.get("source_company_name") or row.get("company_name") or "").strip(),
                    company_domain=(row.get("source_company_domain") or row.get("company_domain") or "").strip(),
                    company_linkedin_url=(row.get("company_linkedin_url") or "").strip(),
                    source_query=(row.get("rubric_persona") or row.get("source_role_segment") or row.get("source_role_segments") or "").strip(),
                )

        for row in companies_rows:
            register(
                company_slug=(row.get("linkedin_slug") or "").strip(),
                company_name=(row.get("company_name") or "").strip(),
                company_domain=(row.get("company_domain") or row.get("resolved_domain") or "").strip(),
                company_linkedin_url=(row.get("company_linkedin_url") or row.get("linkedin_url") or "").strip(),
                source_query=(row.get("source_query") or "").strip(),
            )

        targets = sorted(
            targets_by_key.values(),
            key=lambda item: (
                item.company_name.lower(),
                item.company_domain.lower(),
            ),
        )
        if max_companies > 0:
            return targets[:max_companies]
        return targets

    async def _research_companies(
        self,
        *,
        paths: RunPaths,
        targets: list[ResearchCompanyTarget],
        policy: CompanyResearchPolicy,
        seller: str,
        segment: str,
        seller_context: str,
        campaign_context: str,
        budget_tracker: RunBudgetTracker,
        reasoner: MiniMaxCompanyReasoner | None,
        llm_jsonl_path: Path,
    ) -> list[CompanyRow]:
        semaphore = asyncio.Semaphore(6)

        async def worker(target: ResearchCompanyTarget) -> CompanyRow:
            async with semaphore:
                company_payload: dict[str, Any] = {}
                company_posts_payload: dict[str, Any] | None = None
                if target.company_linkedin_url:
                    try:
                        company_response = await self._harvest.get_company(url=target.company_linkedin_url)
                        budget_tracker.record_harvest_company_get(
                            stage="company_research_company_get",
                            company_url=target.company_linkedin_url,
                        )
                        append_jsonl(
                            paths.harvest_company_jsonl,
                            {
                                "provider": "harvest",
                                "stage": "company_research_company_get",
                                "company_url": target.company_linkedin_url,
                                "response": company_response.data,
                            },
                        )
                        if isinstance(company_response.data, dict):
                            company_payload = company_response.data
                    except Exception as exc:
                        append_jsonl(
                            paths.harvest_company_jsonl,
                            {
                                "provider": "harvest",
                                "stage": "company_research_company_get_error",
                                "company_url": target.company_linkedin_url,
                                "error": str(exc),
                            },
                        )

                    if policy.include_company_posts:
                        try:
                            posts_response = await self._harvest.company_posts(
                                company=target.company_linkedin_url,
                                posted_limit=policy.posted_limit,
                                page=1,
                            )
                            budget_tracker.record_harvest_company_posts(
                                stage="company_research_company_posts",
                                company_url=target.company_linkedin_url,
                                page=1,
                            )
                            append_jsonl(
                                paths.harvest_company_jsonl,
                                {
                                    "provider": "harvest",
                                    "stage": "company_research_company_posts",
                                    "company_url": target.company_linkedin_url,
                                    "response": posts_response.data,
                                },
                            )
                            if isinstance(posts_response.data, dict):
                                company_posts_payload = posts_response.data
                        except Exception as exc:
                            append_jsonl(
                                paths.harvest_company_jsonl,
                                {
                                    "provider": "harvest",
                                    "stage": "company_research_company_posts_error",
                                    "company_url": target.company_linkedin_url,
                                    "error": str(exc),
                                },
                            )

                company_name = self._company_name(company_payload) or target.company_name
                company_domain = self._company_domain(company_payload) or target.company_domain
                if not company_domain and company_name:
                    try:
                        company_domain = await self._fallback_company_domain(
                            paths=paths,
                            company_name=company_name,
                            budget_tracker=budget_tracker,
                        )
                    except Exception as exc:
                        append_jsonl(
                            paths.serper_jsonl,
                            {
                                "provider": "serper",
                                "stage": "company_research_domain_fallback_error",
                                "company_name": company_name,
                                "error": str(exc),
                            },
                        )

                pages: list[ResearchPage] = []
                news_payload: dict[str, Any] | None = None
                if company_domain:
                    try:
                        pages = await self._collect_research_pages(
                            paths=paths,
                            company_domain=company_domain,
                            crawler_provider=policy.crawler_provider,
                            brightdata_domain_discovery=policy.brightdata_domain_discovery,
                            max_pages=policy.max_research_pages,
                            timeout_ms=policy.research_page_timeout_ms,
                            budget_tracker=budget_tracker,
                        )
                    except Exception as exc:
                        append_jsonl(
                            paths.firecrawl_jsonl,
                            {
                                "provider": policy.crawler_provider,
                                "stage": "company_research_page_collection_error",
                                "company_name": company_name,
                                "company_domain": company_domain,
                                "error": str(exc),
                            },
                        )
                    try:
                        news_response = await self._serper.news(
                            f'"{company_name}" "{company_domain}"',
                            num=5,
                        )
                        budget_tracker.record_serper_search(
                            stage="company_research_news",
                            query=f'"{company_name}" "{company_domain}"',
                            search_type="news",
                            num=5,
                        )
                        append_jsonl(
                            paths.serper_jsonl,
                            {
                                "provider": "serper",
                                "stage": "company_research_news",
                                "company_name": company_name,
                                "company_domain": company_domain,
                                "response": news_response.data,
                            },
                        )
                        if isinstance(news_response.data, dict):
                            news_payload = news_response.data
                    except Exception as exc:
                        append_jsonl(
                            paths.serper_jsonl,
                            {
                                "provider": "serper",
                                "stage": "company_research_news_error",
                                "company_name": company_name,
                                "company_domain": company_domain,
                                "error": str(exc),
                            },
                        )

                research = summarize_company_research(
                    company_payload=company_payload,
                    company_posts_payload=company_posts_payload,
                    news_payload=news_payload,
                    pages=pages,
                )
                if reasoner is not None:
                    try:
                        reasoning_result = await reasoner.summarize(
                            seller=seller,
                            segment=segment,
                            seller_context=seller_context,
                            campaign_context=campaign_context,
                            company_payload=company_payload,
                            company_posts_payload=company_posts_payload,
                            news_payload=news_payload,
                            pages=pages,
                        )
                        usage = reasoning_result.raw_response.get("usage")
                        usage_payload = usage if isinstance(usage, dict) else {}
                        budget_tracker.record_minimax_company_reasoning(
                            stage="company_research_reasoning",
                            company_name=company_name,
                            model=str(reasoning_result.raw_response.get("model") or ""),
                            prompt_tokens=int(usage_payload.get("prompt_tokens") or 0),
                            completion_tokens=int(usage_payload.get("completion_tokens") or 0),
                        )
                        append_jsonl(
                            llm_jsonl_path,
                            {
                                "provider": "minimax",
                                "stage": "company_research_reasoning",
                                "company_name": company_name,
                                "company_domain": company_domain,
                                "raw_response": reasoning_result.raw_response,
                                "parsed_response": reasoning_result.parsed_response,
                            },
                        )
                        research = reasoning_result.summary
                    except Exception as exc:
                        append_jsonl(
                            llm_jsonl_path,
                            {
                                "provider": "minimax",
                                "stage": "company_research_reasoning_error",
                                "company_name": company_name,
                                "company_domain": company_domain,
                                "error": str(exc),
                            },
                        )

                return self._build_company_row(
                    naming=paths.naming,
                    source_query=target.source_query,
                    company_name=company_name,
                    company_domain=company_domain,
                    company_linkedin_url=target.company_linkedin_url,
                    company_payload=company_payload,
                    research=research,
                    company_posts_payload=company_posts_payload,
                    pages=pages,
                    crawler_provider=policy.crawler_provider,
                )

        return await asyncio.gather(*(worker(target) for target in targets))

    async def _fallback_company_domain(
        self,
        *,
        paths: RunPaths,
        company_name: str,
        budget_tracker: RunBudgetTracker,
    ) -> str:
        response = await self._serper.search(f'"{company_name}" official website', num=5)
        budget_tracker.record_serper_search(
            stage="company_research_domain_fallback",
            query=f'"{company_name}" official website',
            num=5,
        )
        append_jsonl(
            paths.serper_jsonl,
            {
                "provider": "serper",
                "stage": "company_research_domain_fallback",
                "query": f'"{company_name}" official website',
                "response": response.data,
            },
        )
        if not isinstance(response.data, dict):
            return ""
        organic = response.data.get("organic")
        if not isinstance(organic, list):
            return ""
        for item in organic:
            if not isinstance(item, dict):
                continue
            candidate = item.get("link") or item.get("url")
            if not isinstance(candidate, str):
                continue
            parsed = urlparse(candidate)
            if parsed.netloc and "linkedin.com" not in parsed.netloc.lower():
                return parsed.netloc.lower().removeprefix("www.")
        return ""

    async def _collect_research_pages(
        self,
        *,
        paths: RunPaths,
        company_domain: str,
        crawler_provider: str,
        brightdata_domain_discovery: bool,
        max_pages: int,
        timeout_ms: int,
        budget_tracker: RunBudgetTracker,
    ) -> list[ResearchPage]:
        if crawler_provider == "brightdata":
            return await self._collect_research_pages_brightdata(
                paths=paths,
                company_domain=company_domain,
                domain_discovery=brightdata_domain_discovery,
                max_pages=max_pages,
                budget_tracker=budget_tracker,
            )

        if self._firecrawl is None:
            return []
        homepage = f"https://{company_domain}/"
        map_response = await self._firecrawl.map(
            homepage,
            limit=200,
            include_subdomains=False,
            sitemap="include",
        )
        budget_tracker.record_firecrawl_map(
            stage="company_research_map",
            company_domain=company_domain,
        )
        append_jsonl(
            paths.firecrawl_jsonl,
            {
                "provider": "firecrawl",
                "stage": "company_research_map",
                "company_domain": company_domain,
                "response": map_response.data,
            },
        )
        map_payload = map_response.data if isinstance(map_response.data, dict) else {}
        target_urls = select_research_urls(homepage, map_payload, max_pages=max_pages)

        async def scrape_url(target_url: str) -> ResearchPage:
            response = await self._firecrawl.scrape(
                target_url,
                formats=["markdown"],
                only_main_content=True,
                timeout=timeout_ms,
            )
            budget_tracker.record_firecrawl_scrape(
                stage="company_research_scrape",
                url=target_url,
            )
            append_jsonl(
                paths.firecrawl_jsonl,
                {
                    "provider": "firecrawl",
                    "stage": "company_research_scrape",
                    "company_domain": company_domain,
                    "url": target_url,
                    "response": response.data,
                },
            )
            return self._page_from_firecrawl_payload(target_url, response.data)

        return await asyncio.gather(*(scrape_url(url) for url in target_urls))

    async def _collect_research_pages_brightdata(
        self,
        *,
        paths: RunPaths,
        company_domain: str,
        domain_discovery: bool,
        max_pages: int,
        budget_tracker: RunBudgetTracker,
    ) -> list[ResearchPage]:
        if self._brightdata is None:
            return []
        homepage = f"https://{company_domain}/"
        if not domain_discovery:
            target_urls = guess_research_urls(homepage, max_pages=max_pages)
            response = await self._brightdata.scrape_urls(target_urls)
            budget_tracker.record_brightdata_crawl(
                stage="company_research_brightdata_crawl",
                urls=target_urls,
                dataset_id=self._brightdata._settings.crawl_dataset_id,
            )
            append_jsonl(
                paths.firecrawl_jsonl,
                {
                    "provider": "brightdata",
                    "stage": "company_research_brightdata_crawl",
                    "company_domain": company_domain,
                    "urls": target_urls,
                    "response": response.data,
                },
            )
            return self._pages_from_brightdata_payload(response.data, fallback_urls=target_urls)

        discovery_urls = brightdata_discovery_seed_urls(homepage)
        discovery_response = await self._brightdata.scrape_urls(discovery_urls)
        budget_tracker.record_brightdata_crawl(
            stage="company_research_brightdata_discovery",
            urls=discovery_urls,
            dataset_id=self._brightdata._settings.crawl_dataset_id,
        )
        append_jsonl(
            paths.firecrawl_jsonl,
            {
                "provider": "brightdata",
                "stage": "company_research_brightdata_discovery",
                "company_domain": company_domain,
                "urls": discovery_urls,
                "response": discovery_response.data,
            },
        )
        discovery_pages = self._pages_from_brightdata_payload(discovery_response.data, fallback_urls=discovery_urls)
        target_urls = select_brightdata_research_urls(homepage, discovery_pages, max_pages=max_pages)
        append_jsonl(
            paths.firecrawl_jsonl,
            {
                "provider": "brightdata",
                "stage": "company_research_brightdata_selected_urls",
                "company_domain": company_domain,
                "discovery_urls": discovery_urls,
                "selected_urls": target_urls,
            },
        )
        discovery_lookup = {page.url.rstrip("/"): page for page in discovery_pages if page.url}
        remaining_urls = [url for url in target_urls if url.rstrip("/") not in discovery_lookup]
        crawled_pages: list[ResearchPage] = []
        if remaining_urls:
            response = await self._brightdata.scrape_urls(remaining_urls)
            budget_tracker.record_brightdata_crawl(
                stage="company_research_brightdata_crawl",
                urls=remaining_urls,
                dataset_id=self._brightdata._settings.crawl_dataset_id,
            )
            append_jsonl(
                paths.firecrawl_jsonl,
                {
                    "provider": "brightdata",
                    "stage": "company_research_brightdata_crawl",
                    "company_domain": company_domain,
                    "urls": remaining_urls,
                    "response": response.data,
                },
            )
            crawled_pages = self._pages_from_brightdata_payload(response.data, fallback_urls=remaining_urls)

        page_lookup = {page.url.rstrip("/"): page for page in crawled_pages if page.url}
        for key, page in discovery_lookup.items():
            page_lookup.setdefault(key, page)
        ordered_pages: list[ResearchPage] = []
        for url in target_urls:
            page = page_lookup.get(url.rstrip("/"))
            if page is not None:
                ordered_pages.append(page)
        return ordered_pages

    @staticmethod
    def _page_from_firecrawl_payload(url: str, payload: Any) -> ResearchPage:
        if not isinstance(payload, dict):
            return ResearchPage(url=url, title="", description="", markdown="", source="firecrawl")
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return ResearchPage(
            url=url,
            title=str(metadata.get("title") or ""),
            description=str(metadata.get("description") or ""),
            markdown=str(payload.get("markdown") or ""),
            source="firecrawl",
        )

    @staticmethod
    def _pages_from_brightdata_payload(
        payload: Any,
        *,
        fallback_urls: list[str],
    ) -> list[ResearchPage]:
        rows = payload if isinstance(payload, list) else []
        pages: list[ResearchPage] = []
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or (fallback_urls[index] if index < len(fallback_urls) else "")).strip()
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            markdown = str(
                item.get("markdown")
                or item.get("html2text")
                or item.get("page_html")
                or ""
            )
            pages.append(
                ResearchPage(
                    url=url,
                    title=str(item.get("title") or metadata.get("title") or ""),
                    description=str(item.get("description") or metadata.get("description") or ""),
                    markdown=markdown,
                    source="brightdata",
                )
            )
        return pages

    @staticmethod
    def _company_name(payload: dict[str, Any]) -> str:
        element = payload.get("element")
        if not isinstance(element, dict):
            return ""
        return str(element.get("name") or "").strip()

    @staticmethod
    def _company_domain(payload: dict[str, Any]) -> str:
        element = payload.get("element")
        if not isinstance(element, dict):
            return ""
        website = str(element.get("website") or "").strip()
        if not website:
            return ""
        parsed = urlparse(website if "://" in website else f"https://{website}")
        return parsed.netloc.lower().removeprefix("www.")

    @staticmethod
    def _parse_domain(value: str) -> str:
        stripped = (value or "").strip().lower()
        if not stripped:
            return ""
        parsed = urlparse(stripped if "://" in stripped else f"https://{stripped}")
        return parsed.netloc.lower().removeprefix("www.") if parsed.netloc else stripped.removeprefix("www.")

    @staticmethod
    def _default_campaign_context(*, seller: str, segment: str) -> str:
        seller_slug = seller.strip().lower()
        segment_slug = segment.strip().lower()
        if seller_slug == "alertica" and "fqhc" in segment_slug:
            return (
                "Alertica sells agentless file integrity monitoring and compliance monitoring to FQHCs. "
                "Reason about HIPAA exposure, ePHI integrity, lightweight deployment, multi-site clinic operations, "
                "NextGen or EHR footprint, OCR-readiness, and likely operational or compliance bottlenecks."
            )
        return (
            f"Research this company in the context of seller {seller} and segment {segment}. "
            "Infer offer, ICP, painpoints, and notable signals that matter for outbound personalization."
        )

    @staticmethod
    def _build_company_row(
        *,
        naming,
        source_query: str,
        company_name: str,
        company_domain: str,
        company_linkedin_url: str,
        company_payload: dict[str, Any],
        research,
        company_posts_payload: dict[str, Any] | None,
        pages: list[ResearchPage],
        crawler_provider: str,
    ) -> CompanyRow:
        element = company_payload.get("element")
        if not isinstance(element, dict):
            element = {}
        recent_post_summary, recent_post_urls = summarize_company_posts(company_posts_payload)
        row = CompanyRow(
            run_slug=naming.run_slug,
            seller_slug=naming.seller_slug,
            segment_slug=naming.segment_slug,
            source_query=source_query,
            company_name=company_name,
            company_domain=company_domain,
            company_linkedin_url=company_linkedin_url or str(element.get("linkedinUrl") or "").strip(),
            company_description=research.description,
            company_offer=research.offer,
            company_icp=research.icp,
            company_painpoint=research.painpoint,
            company_size=str(element.get("employeeCount") or "").strip(),
            company_headcount_exact=str(element.get("employeeCount") or "").strip(),
            company_headcount_range="",
            company_signals=research.signals,
            company_signal_sources=research.signal_sources,
            company_recent_post_summary=recent_post_summary,
            company_recent_post_urls=recent_post_urls,
            company_research_page_count=str(len([page for page in pages if page.markdown.strip()])),
            company_research_page_urls=" | ".join(page.url for page in pages if page.url),
            company_research_crawler_provider=crawler_provider,
            company_quality_score="0",
            company_needs_followup="true",
            harvest_company_id=str(element.get("id") or "").strip(),
            harvest_status=str(company_payload.get("status") or "").strip(),
        )
        score = company_quality_score(row)
        return CompanyRow(
            **{
                **row.to_dict(),
                "company_quality_score": str(score),
                "company_needs_followup": "true" if score < 70 else "false",
            }
        )

    @staticmethod
    def _write_people_context_csv(
        *,
        input_rows: list[dict[str, str]],
        researched_companies: list[CompanyRow],
        output_path: str | Path,
    ) -> None:
        company_by_slug_or_name: dict[str, CompanyRow] = {}
        for row in researched_companies:
            if row.company_linkedin_url:
                company_by_slug_or_name[row.company_linkedin_url] = row
            if row.company_name:
                company_by_slug_or_name[row.company_name.casefold()] = row

        output_headers = list(input_rows[0].keys()) if input_rows else []
        for header in (
            "company_description",
            "company_offer",
            "company_icp",
            "company_painpoint",
            "company_signals",
            "company_signal_sources",
            "company_recent_post_summary",
            "company_recent_post_urls",
            "company_research_page_count",
            "company_research_page_urls",
            "company_research_crawler_provider",
            "company_quality_score",
            "company_needs_followup",
            "company_research_run_slug",
        ):
            if header not in output_headers:
                output_headers.append(header)

        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=output_headers)
            writer.writeheader()
            for row in input_rows:
                lookup = None
                company_name = (row.get("source_company_name") or row.get("company_name") or "").strip()
                if company_name:
                    lookup = company_by_slug_or_name.get(company_name.casefold())
                payload = dict(row)
                if lookup is not None:
                    payload.update(
                        {
                            "company_description": lookup.company_description,
                            "company_offer": lookup.company_offer,
                            "company_icp": lookup.company_icp,
                            "company_painpoint": lookup.company_painpoint,
                            "company_signals": lookup.company_signals,
                            "company_signal_sources": lookup.company_signal_sources,
                            "company_recent_post_summary": lookup.company_recent_post_summary,
                            "company_recent_post_urls": lookup.company_recent_post_urls,
                            "company_research_page_count": lookup.company_research_page_count,
                            "company_research_page_urls": lookup.company_research_page_urls,
                            "company_research_crawler_provider": lookup.company_research_crawler_provider,
                            "company_quality_score": lookup.company_quality_score,
                            "company_needs_followup": lookup.company_needs_followup,
                            "company_research_run_slug": lookup.run_slug,
                        }
                    )
                writer.writerow(payload)
