from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from listbuild.budget import RunBudgetTracker, ScrapePricing
from listbuild.datasets import (
    CompanyRow,
    company_quality_score,
    company_row_from_harvest,
    write_company_rows_csv,
)
from listbuild.jsonl import append_jsonl
from listbuild.providers import BrightDataClient, FirecrawlClient, HarvestClient, SerperClient
from listbuild.research import (
    ResearchPage,
    guess_research_urls,
    select_research_urls,
    summarize_company_posts,
    summarize_company_research,
)
from listbuild.runs import RunPaths, build_run_paths, scaffold_run_directories
from listbuild.storage import (
    build_company_record_key,
    fetch_existing_company_record_keys,
    sync_run_to_supabase,
)


@dataclass(frozen=True, slots=True)
class CompanyScrapePolicy:
    headcount_buckets: tuple[str, ...] = ("2-10", "11-50", "51-200", "201-500")
    search_results_per_query: int = 10
    max_companies: int = 100
    max_research_pages: int = 8
    crawler_provider: str = "firecrawl"
    include_company_posts: bool = True
    posted_limit: str = "month"
    research_page_timeout_ms: int = 60000
    dedupe_against_supabase: bool = True
    upsert_supabase: bool = False


@dataclass(slots=True)
class CompanyScrapeRun:
    run_slug: str
    company_count: int
    output_csv: str
    raw_serper_jsonl: str
    raw_harvest_company_jsonl: str
    raw_firecrawl_jsonl: str
    budget: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_slug": self.run_slug,
            "company_count": self.company_count,
            "output_csv": self.output_csv,
            "raw_serper_jsonl": self.raw_serper_jsonl,
            "raw_harvest_company_jsonl": self.raw_harvest_company_jsonl,
            "raw_firecrawl_jsonl": self.raw_firecrawl_jsonl,
            "budget": self.budget,
        }


class CompanyScrapeWorkflow:
    def __init__(
        self,
        serper: SerperClient,
        harvest: HarvestClient,
        firecrawl: FirecrawlClient | None = None,
        brightdata: BrightDataClient | None = None,
    ) -> None:
        self._serper = serper
        self._harvest = harvest
        self._firecrawl = firecrawl
        self._brightdata = brightdata

    async def run(
        self,
        *,
        seller: str,
        segment: str,
        query: str,
        date_stamp: str | None = None,
        root: str = "runs",
        policy: CompanyScrapePolicy | None = None,
        budget_tracker: RunBudgetTracker | None = None,
        pricing: ScrapePricing | None = None,
    ) -> CompanyScrapeRun:
        policy = policy or CompanyScrapePolicy()
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

        search_queries = self._build_company_queries(query, policy.headcount_buckets)
        company_urls = await self._discover_company_urls(
            paths=paths,
            search_queries=search_queries,
            search_results_per_query=policy.search_results_per_query,
            max_companies=policy.max_companies,
            budget_tracker=tracker,
        )
        if policy.dedupe_against_supabase:
            company_urls = self._filter_existing_company_urls(company_urls)

        companies = await self._enrich_companies(
            paths=paths,
            source_query=query,
            company_urls=company_urls,
            policy=policy,
            budget_tracker=tracker,
        )
        write_company_rows_csv(companies, paths.company_csv)

        if policy.upsert_supabase:
            sync_run_to_supabase(
                paths=paths,
                metadata={
                    "workflow": "company-scrape",
                    "query": query,
                    "company_count": len(companies),
                },
            )
        budget_summary = tracker.write_summary()

        return CompanyScrapeRun(
            run_slug=paths.naming.run_slug,
            company_count=len(companies),
            output_csv=str(paths.company_csv),
            raw_serper_jsonl=str(paths.serper_jsonl),
            raw_harvest_company_jsonl=str(paths.harvest_company_jsonl),
            raw_firecrawl_jsonl=str(paths.firecrawl_jsonl),
            budget=budget_summary,
        )

    @staticmethod
    def _build_company_queries(base_query: str, headcount_buckets: tuple[str, ...]) -> list[str]:
        if not headcount_buckets:
            return [f'site:linkedin.com/company "{base_query}"']
        queries: list[str] = []
        for bucket in headcount_buckets:
            queries.append(f'site:linkedin.com/company "{base_query}" "{bucket}"')
        return queries

    async def _discover_company_urls(
        self,
        *,
        paths: RunPaths,
        search_queries: list[str],
        search_results_per_query: int,
        max_companies: int,
        budget_tracker: RunBudgetTracker,
    ) -> list[str]:
        seen: set[str] = set()
        results: list[str] = []

        async def run_query(search_query: str) -> None:
            response = await self._serper.search(search_query, num=search_results_per_query)
            budget_tracker.record_serper_search(
                stage="company_discovery",
                query=search_query,
                num=search_results_per_query,
            )
            append_jsonl(
                paths.serper_jsonl,
                {
                    "provider": "serper",
                    "stage": "company_discovery",
                    "query": search_query,
                    "response": response.data,
                },
            )
            for url in self._extract_linkedin_company_urls(response.data):
                if url in seen:
                    continue
                seen.add(url)
                results.append(url)

        await asyncio.gather(*(run_query(query) for query in search_queries))
        return results[:max_companies]

    @staticmethod
    def _extract_linkedin_company_urls(payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        organic = payload.get("organic")
        if not isinstance(organic, list):
            return []
        urls: list[str] = []
        for item in organic:
            if not isinstance(item, dict):
                continue
            candidate = item.get("link") or item.get("url")
            if not isinstance(candidate, str):
                continue
            parsed = urlparse(candidate)
            if "linkedin.com" not in parsed.netloc.lower():
                continue
            if "/company/" not in parsed.path.lower():
                continue
            urls.append(candidate)
        return urls

    async def _enrich_companies(
        self,
        *,
        paths: RunPaths,
        source_query: str,
        company_urls: list[str],
        policy: CompanyScrapePolicy,
        budget_tracker: RunBudgetTracker,
    ) -> list[CompanyRow]:
        semaphore = asyncio.Semaphore(8)

        async def worker(company_url: str) -> CompanyRow:
            async with semaphore:
                company_response = await self._harvest.get_company(url=company_url)
                budget_tracker.record_harvest_company_get(
                    stage="company_get",
                    company_url=company_url,
                )
                append_jsonl(
                    paths.harvest_company_jsonl,
                    {
                        "provider": "harvest",
                        "stage": "company_get",
                        "company_url": company_url,
                        "response": company_response.data,
                    },
                )
                company_payload = company_response.data if isinstance(company_response.data, dict) else {}
                company_posts_payload: dict[str, Any] | None = None
                if policy.include_company_posts:
                    posts_response = await self._harvest.company_posts(
                        company=company_url,
                        posted_limit=policy.posted_limit,
                        page=1,
                    )
                    budget_tracker.record_harvest_company_posts(
                        stage="company_posts",
                        company_url=company_url,
                        page=1,
                    )
                    append_jsonl(
                        paths.harvest_company_jsonl,
                        {
                            "provider": "harvest",
                            "stage": "company_posts",
                            "company_url": company_url,
                            "response": posts_response.data,
                        },
                    )
                    if isinstance(posts_response.data, dict):
                        company_posts_payload = posts_response.data

                company_name = self._company_name(company_payload)
                company_domain = self._company_domain(company_payload)
                if not company_domain and company_name:
                    company_domain = await self._fallback_company_domain(
                        paths=paths,
                        company_name=company_name,
                        budget_tracker=budget_tracker,
                    )

                pages: list[ResearchPage] = []
                news_payload: dict[str, Any] | None = None
                if company_domain:
                    pages = await self._collect_research_pages(
                        paths=paths,
                        company_domain=company_domain,
                        crawler_provider=policy.crawler_provider,
                        max_pages=policy.max_research_pages,
                        timeout_ms=policy.research_page_timeout_ms,
                        budget_tracker=budget_tracker,
                    )
                    news_response = await self._serper.news(
                        f'"{company_name}" "{company_domain}"',
                        num=5,
                    )
                    budget_tracker.record_serper_search(
                        stage="company_news",
                        query=f'"{company_name}" "{company_domain}"',
                        search_type="news",
                        num=5,
                    )
                    append_jsonl(
                        paths.serper_jsonl,
                        {
                            "provider": "serper",
                            "stage": "company_news",
                            "company_name": company_name,
                            "company_domain": company_domain,
                            "response": news_response.data,
                        },
                    )
                    if isinstance(news_response.data, dict):
                        news_payload = news_response.data

                research = summarize_company_research(
                    company_payload=company_payload,
                    company_posts_payload=company_posts_payload,
                    news_payload=news_payload,
                    pages=pages,
                )
                return self._build_company_row(
                    naming=paths.naming,
                    source_query=source_query,
                    company_payload=company_payload,
                    research=research,
                    company_posts_payload=company_posts_payload,
                    company_domain_override=company_domain,
                )

        return await asyncio.gather(*(worker(url) for url in company_urls))

    @staticmethod
    def _filter_existing_company_urls(company_urls: list[str]) -> list[str]:
        keys = [
            build_company_record_key(company_linkedin_url=company_url)
            for company_url in company_urls
        ]
        existing = fetch_existing_company_record_keys(keys)
        if not existing:
            return company_urls
        return [
            company_url
            for company_url in company_urls
            if build_company_record_key(company_linkedin_url=company_url) not in existing
        ]

    async def _fallback_company_domain(
        self,
        *,
        paths: RunPaths,
        company_name: str,
        budget_tracker: RunBudgetTracker,
    ) -> str:
        response = await self._serper.search(f'"{company_name}" official website', num=5)
        budget_tracker.record_serper_search(
            stage="company_domain_fallback",
            query=f'"{company_name}" official website',
            num=5,
        )
        append_jsonl(
            paths.serper_jsonl,
            {
                "provider": "serper",
                "stage": "company_domain_fallback",
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
        max_pages: int,
        timeout_ms: int,
        budget_tracker: RunBudgetTracker,
    ) -> list[ResearchPage]:
        if crawler_provider == "brightdata":
            return await self._collect_research_pages_brightdata(
                paths=paths,
                company_domain=company_domain,
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
            stage="company_map",
            company_domain=company_domain,
        )
        append_jsonl(
            paths.firecrawl_jsonl,
            {
                "provider": "firecrawl",
                "stage": "company_map",
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
                stage="company_scrape",
                url=target_url,
            )
            append_jsonl(
                paths.firecrawl_jsonl,
                {
                    "provider": "firecrawl",
                    "stage": "company_scrape",
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
        max_pages: int,
        budget_tracker: RunBudgetTracker,
    ) -> list[ResearchPage]:
        if self._brightdata is None:
            return []
        homepage = f"https://{company_domain}/"
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

    @staticmethod
    def _page_from_firecrawl_payload(url: str, payload: Any) -> ResearchPage:
        if not isinstance(payload, dict):
            return ResearchPage(url=url, title="", description="", markdown="")
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
            markdown = str(
                item.get("markdown")
                or item.get("html2text")
                or item.get("page_html")
                or ""
            )
            pages.append(
                ResearchPage(
                    url=url,
                    title="",
                    description="",
                    markdown=markdown,
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
    def _build_company_row(
        *,
        naming,
        source_query: str,
        company_payload: dict[str, Any],
        research,
        company_posts_payload: dict[str, Any] | None,
        company_domain_override: str,
    ) -> CompanyRow:
        base_row = company_row_from_harvest(
            company_payload,
            naming=naming,
            source_query=source_query,
        )
        recent_post_summary, recent_post_urls = summarize_company_posts(company_posts_payload)
        enriched = CompanyRow(
            **{
                **base_row.to_dict(),
                "company_domain": company_domain_override or base_row.company_domain,
                "company_description": research.description or base_row.company_description,
                "company_offer": research.offer,
                "company_icp": research.icp,
                "company_painpoint": research.painpoint,
                "company_signals": research.signals,
                "company_signal_sources": research.signal_sources,
                "company_recent_post_summary": recent_post_summary,
                "company_recent_post_urls": recent_post_urls,
            }
        )
        score = company_quality_score(enriched)
        return CompanyRow(
            **{
                **enriched.to_dict(),
                "company_quality_score": str(min(score, 100)),
                "company_needs_followup": "true" if score < 70 else "false",
            }
        )
