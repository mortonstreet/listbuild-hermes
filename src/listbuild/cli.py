from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Any

from listbuild.budget import RunBudgetTracker, ScrapePricing
from listbuild.config import AppSettings, load_local_env, load_settings
from listbuild.datasets import (
    materialize_harvest_companies_to_csv,
    materialize_harvest_profiles_to_csv,
)
from listbuild.http import ApiResponse
from listbuild.importers import import_companies_csv, import_people_csv
from listbuild.pipeline import (
    build_pipeline_blueprint,
    build_coverage_plan,
    estimate_pipeline_cost,
    load_pipeline_template,
)
from listbuild.providers import (
    BrightDataClient,
    FirecrawlClient,
    HarvestClient,
    MiniMaxClient,
    MillionVerifierClient,
    ProspeoClient,
    SerperClient,
)
from listbuild.runs import build_run_paths, scaffold_run_directories
from listbuild.storage import apply_schema_file, ping_postgres, sync_run_to_supabase
from listbuild.workflows import (
    AlerticaFqhcQualifierPolicy,
    AlerticaFqhcQualifierWorkflow,
    CampaignPrepPolicy,
    CampaignPrepWorkflow,
    CampaignReviewPolicy,
    CampaignReviewWorkflow,
    CompanyDiscoveryPolicy,
    CompanyDiscoveryWorkflow,
    CompanyResearchPolicy,
    CompanyResearchWorkflow,
    CompanyScrapePolicy,
    CompanyScrapeWorkflow,
    PeopleScrapePolicy,
    PeopleScrapeWorkflow,
    RoleSearchSegment,
)


def _load_json_value(
    *,
    payload_file: str | None = None,
    payload_json: str | None = None,
) -> dict[str, Any]:
    if payload_file:
        return json.loads(Path(payload_file).read_text(encoding="utf-8"))
    if payload_json:
        return json.loads(payload_json)
    raise ValueError("One of --payload-file or --payload-json is required")


def _load_optional_json_value(
    *,
    payload_file: str | None = None,
    payload_json: str | None = None,
) -> dict[str, Any]:
    if not payload_file and not payload_json:
        return {}
    return _load_json_value(payload_file=payload_file, payload_json=payload_json)


def _parse_role_segment(raw_value: str) -> RoleSearchSegment:
    if ":" not in raw_value:
        raise ValueError(
            "Role segments must use the format 'segment-name:title1|title2|title3'"
        )
    segment_name, raw_titles = raw_value.split(":", 1)
    titles = tuple(title.strip() for title in raw_titles.split("|") if title.strip())
    if not segment_name.strip() or not titles:
        raise ValueError(
            "Role segments must include a segment name and at least one title"
        )
    return RoleSearchSegment(
        segment_name=segment_name.strip(),
        target_titles=titles,
    )


def _load_pricing(path: str | None) -> ScrapePricing:
    if not path:
        return ScrapePricing()
    return ScrapePricing.from_file(path)


def _configure_workflow_api_audit_log(paths) -> str:
    existing = os.environ.get("LISTBUILD_API_AUDIT_LOG", "").strip()
    if existing:
        return existing
    audit_path = paths.raw_dir / f"{paths.naming.run_slug}-api-call-audit.jsonl"
    os.environ.setdefault("LISTBUILD_API_AUDIT", "1")
    os.environ["LISTBUILD_API_AUDIT_LOG"] = str(audit_path)
    return str(audit_path)


def _print_response(response: ApiResponse, output_path: str | None = None) -> None:
    payload = {
        "provider": response.provider,
        "status_code": response.status_code,
        "rate_limit": asdict(response.rate_limit),
        "data": response.data,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if output_path:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
        return
    print(rendered)


def _write_bytes(content: bytes, output_path: str) -> None:
    Path(output_path).write_bytes(content)


def _render_json(payload: dict[str, Any], output_path: str | None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if output_path:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
        return
    print(rendered)


async def _run_serper(args: argparse.Namespace, settings: AppSettings) -> None:
    async with SerperClient(settings.serper) as client:
        if args.serper_command == "search":
            response = await client.search(
                args.query,
                gl=args.gl,
                hl=args.hl,
                num=args.num,
                page=args.page,
                autocorrect=args.autocorrect,
            )
        elif args.serper_command == "news":
            response = await client.news(
                args.query,
                gl=args.gl,
                hl=args.hl,
                num=args.num,
                page=args.page,
            )
        elif args.serper_command == "images":
            response = await client.images(
                args.query,
                gl=args.gl,
                hl=args.hl,
                num=args.num,
                page=args.page,
            )
        elif args.serper_command == "places":
            response = await client.places(
                args.query,
                gl=args.gl,
                hl=args.hl,
                page=args.page,
            )
        else:
            payload = _load_json_value(
                payload_file=args.payload_file,
                payload_json=args.payload_json,
            )
            response = await client.raw(args.endpoint, payload)
    _print_response(response, args.output)


def _non_empty_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


async def _run_harvest(args: argparse.Namespace, settings: AppSettings) -> None:
    async with HarvestClient(settings.harvest) as client:
        if args.harvest_command == "company-search":
            response = await client.company_search(
                **_non_empty_params(
                    {
                        "search": args.search,
                        "location": args.location,
                        "geoId": args.geo_id,
                        "industryId": args.industry_id,
                        "companySize": args.company_size,
                        "page": args.page,
                    }
                )
            )
        elif args.harvest_command == "profile-search":
            response = await client.profile_search(
                **_non_empty_params(
                    {
                        "search": args.search,
                        "currentCompany": args.current_company,
                        "pastCompany": args.past_company,
                        "school": args.school,
                        "title": args.title,
                        "location": args.location,
                        "geoId": args.geo_id,
                        "industryId": args.industry_id,
                        "firstName": args.first_name,
                        "lastName": args.last_name,
                        "page": args.page,
                    }
                )
            )
        elif args.harvest_command == "geo-id-search":
            response = await client.geo_id_search(args.search)
        else:
            params = {}
            if args.payload_file or args.payload_json:
                params = _load_json_value(
                    payload_file=args.payload_file,
                    payload_json=args.payload_json,
                )
            response = await client.raw(args.path, params=params)
    _print_response(response, args.output)


async def _run_million(args: argparse.Namespace, settings: AppSettings) -> None:
    async with MillionVerifierClient(settings.million_verifier) as client:
        if args.million_command == "verify":
            response = await client.verify_email(args.email, timeout=args.timeout)
            _print_response(response, args.output)
            return
        if args.million_command == "credits":
            response = await client.credits()
            _print_response(response, args.output)
            return
        if args.million_command == "upload":
            response = await client.upload_file(args.file_path)
            _print_response(response, args.output)
            return
        if args.million_command == "bulk-file-info":
            response = await client.bulk_file_info(args.file_id)
            _print_response(response, args.output)
            return
        if args.million_command == "download-report":
            response = await client.download_report(
                args.file_id,
                filter_status=args.filter_status,
                statuses=args.statuses,
                free=args.free,
                role=args.role,
            )
            _write_bytes(response.data, args.output)
            return
        if args.million_command == "stop-bulk-file":
            response = await client.stop_bulk_file(args.file_id)
            _print_response(response, args.output)
            return
        response = await client.delete_bulk_file(args.file_id)
        _print_response(response, args.output)


async def _run_prospeo(args: argparse.Namespace, settings: AppSettings) -> None:
    async with ProspeoClient(settings.prospeo) as client:
        if args.prospeo_command == "account-information":
            response = await client.account_information()
        elif args.prospeo_command == "search-person":
            payload = _load_json_value(
                payload_file=args.payload_file,
                payload_json=args.payload_json,
            )
            response = await client.search_person(payload)
        elif args.prospeo_command == "search-company":
            payload = _load_json_value(
                payload_file=args.payload_file,
                payload_json=args.payload_json,
            )
            response = await client.search_company(payload)
        elif args.prospeo_command == "enrich-person":
            payload = _load_json_value(
                payload_file=args.payload_file,
                payload_json=args.payload_json,
            )
            response = await client.enrich_person(payload)
        elif args.prospeo_command == "enrich-company":
            payload = _load_json_value(
                payload_file=args.payload_file,
                payload_json=args.payload_json,
            )
            response = await client.enrich_company(payload)
        else:
            payload = None
            if args.payload_file or args.payload_json:
                payload = _load_json_value(
                    payload_file=args.payload_file,
                    payload_json=args.payload_json,
                )
            response = await client.raw(
                args.method,
                args.path,
                payload=payload,
                category=args.category,
            )
    _print_response(response, args.output)


async def _run_firecrawl(args: argparse.Namespace, settings: AppSettings) -> None:
    async with FirecrawlClient(settings.firecrawl) as client:
        if args.firecrawl_command == "scrape":
            response = await client.scrape(
                args.url,
                formats=args.format,
                only_main_content=args.only_main_content,
                timeout=args.timeout,
                wait_for=args.wait_for,
                max_age=args.max_age,
                country=args.country,
            )
        elif args.firecrawl_command == "map":
            response = await client.map(
                args.url,
                search=args.search,
                limit=args.limit,
                include_subdomains=args.include_subdomains,
                sitemap=args.sitemap,
            )
        elif args.firecrawl_command == "search":
            scrape_options = None
            if args.scrape_format:
                scrape_options = {"formats": args.scrape_format}
            response = await client.search(
                args.query,
                limit=args.limit,
                country=args.country,
                location=args.location,
                tbs=args.tbs,
                sources=args.source,
                scrape_options=scrape_options,
            )
        else:
            payload = None
            if args.payload_file or args.payload_json:
                payload = _load_json_value(
                    payload_file=args.payload_file,
                    payload_json=args.payload_json,
                )
            response = await client.raw(
                args.method,
                args.path,
                payload=payload,
                category=args.category,
            )
    _print_response(response, args.output)


async def _run_workflow(args: argparse.Namespace, settings: AppSettings) -> None:
    if args.workflow_command == "company-discovery":
        workflow = CompanyDiscoveryWorkflow(settings)
        run = await workflow.run_query(
            args.query,
            policy=CompanyDiscoveryPolicy(
                search_results=args.search_results,
                max_companies=args.max_companies,
                scrape_homepages=args.scrape,
                scrape_formats=tuple(args.format or ["markdown"]),
                scrape_only_main_content=args.only_main_content,
            ),
        )
        _render_json(run.to_dict(), args.output)
        return

    if args.workflow_command == "company-scrape":
        pricing = _load_pricing(args.pricing_file)
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        _configure_workflow_api_audit_log(paths)
        async with AsyncExitStack() as stack:
            serper = await stack.enter_async_context(SerperClient(settings.serper))
            harvest = await stack.enter_async_context(HarvestClient(settings.harvest))
            firecrawl: FirecrawlClient | None = None
            brightdata: BrightDataClient | None = None
            if args.crawler_provider == "firecrawl":
                firecrawl = await stack.enter_async_context(FirecrawlClient(settings.firecrawl))
            else:
                if settings.brightdata is None:
                    raise ValueError(
                        "Bright Data is not configured. Add BRIGHTDATA_API_KEY and BRIGHTDATA_CRAWL_DATASET_ID to .env."
                    )
                brightdata = await stack.enter_async_context(BrightDataClient(settings.brightdata))

            workflow = CompanyScrapeWorkflow(serper, harvest, firecrawl, brightdata)
            run = await workflow.run(
                seller=args.seller,
                segment=args.segment,
                query=args.query,
                date_stamp=args.date,
                root=args.root,
                policy=CompanyScrapePolicy(
                    headcount_buckets=tuple(
                        args.headcount_bucket or CompanyScrapePolicy().headcount_buckets
                    ),
                    search_results_per_query=args.search_results_per_query,
                    max_companies=args.max_companies,
                    max_research_pages=args.max_research_pages,
                    crawler_provider=args.crawler_provider,
                    include_company_posts=args.include_company_posts,
                    posted_limit=args.posted_limit,
                    research_page_timeout_ms=args.research_page_timeout_ms,
                    dedupe_against_supabase=args.dedupe_supabase,
                    upsert_supabase=args.upsert_supabase,
                ),
                pricing=pricing,
            )
        _render_json(run.to_dict(), args.output)
        return

    if args.workflow_command == "people-scrape":
        role_segments = [_parse_role_segment(value) for value in args.role_segment]
        pricing = _load_pricing(args.pricing_file)
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        _configure_workflow_api_audit_log(paths)
        async with (
            SerperClient(settings.serper) as serper,
            HarvestClient(settings.harvest) as harvest,
        ):
            workflow = PeopleScrapeWorkflow(serper, harvest)
            run = await workflow.run(
                seller=args.seller,
                segment=args.segment,
                companies_csv=args.companies_csv,
                role_segments=role_segments,
                date_stamp=args.date,
                root=args.root,
                policy=PeopleScrapePolicy(
                    max_people_per_company=args.max_people_per_company,
                    max_people_per_company_under_200=args.max_people_per_company_under_200,
                    max_people_per_company_over_200=args.max_people_per_company_over_200,
                    search_results_per_query=args.search_results_per_query,
                    posted_limit=args.posted_limit,
                    include_email=args.include_email,
                    use_main_profile=args.use_main_profile,
                    require_current_employer_match=args.require_current_employer_match,
                    use_company_domain_in_query=args.use_company_domain_in_query,
                    dedupe_against_supabase=args.dedupe_supabase,
                    upsert_supabase=args.upsert_supabase,
                ),
                pricing=pricing,
            )
        _render_json(run.to_dict(), args.output)
        return

    if args.workflow_command == "full-scrape":
        role_segments = [_parse_role_segment(value) for value in args.role_segment]
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        _configure_workflow_api_audit_log(paths)
        pricing = _load_pricing(args.pricing_file)
        budget_tracker = RunBudgetTracker(
            run_slug=paths.naming.run_slug,
            events_path=paths.budget_jsonl,
            summary_path=paths.budget_summary,
            pricing=pricing,
        )
        company_policy = CompanyScrapePolicy(
            headcount_buckets=tuple(
                args.headcount_bucket or CompanyScrapePolicy().headcount_buckets
            ),
            search_results_per_query=args.search_results_per_query,
            max_companies=args.max_companies,
            max_research_pages=args.max_research_pages,
            crawler_provider=args.crawler_provider,
            include_company_posts=args.include_company_posts,
            posted_limit=args.posted_limit,
            research_page_timeout_ms=args.research_page_timeout_ms,
            dedupe_against_supabase=args.dedupe_supabase,
            upsert_supabase=False,
        )
        people_policy = PeopleScrapePolicy(
            max_people_per_company=args.max_people_per_company,
            search_results_per_query=args.people_search_results_per_query,
            posted_limit=args.people_posted_limit,
            include_email=args.include_email,
            use_main_profile=args.use_main_profile,
            dedupe_against_supabase=args.dedupe_supabase,
            upsert_supabase=args.upsert_supabase,
        )
        async with AsyncExitStack() as stack:
            serper = await stack.enter_async_context(SerperClient(settings.serper))
            harvest = await stack.enter_async_context(HarvestClient(settings.harvest))
            firecrawl: FirecrawlClient | None = None
            brightdata: BrightDataClient | None = None
            if args.crawler_provider == "firecrawl":
                firecrawl = await stack.enter_async_context(FirecrawlClient(settings.firecrawl))
            else:
                if settings.brightdata is None:
                    raise ValueError(
                        "Bright Data is not configured. Add BRIGHTDATA_API_KEY and BRIGHTDATA_CRAWL_DATASET_ID to .env."
                    )
                brightdata = await stack.enter_async_context(BrightDataClient(settings.brightdata))

            company_workflow = CompanyScrapeWorkflow(serper, harvest, firecrawl, brightdata)
            company_run = await company_workflow.run(
                seller=args.seller,
                segment=args.segment,
                query=args.query,
                date_stamp=paths.naming.date_stamp,
                root=args.root,
                policy=company_policy,
                budget_tracker=budget_tracker,
            )
            people_workflow = PeopleScrapeWorkflow(serper, harvest)
            people_run = await people_workflow.run(
                seller=args.seller,
                segment=args.segment,
                companies_csv=company_run.output_csv,
                role_segments=role_segments,
                date_stamp=paths.naming.date_stamp,
                root=args.root,
                policy=people_policy,
                budget_tracker=budget_tracker,
            )
        _render_json(
            {
                "company_run": company_run.to_dict(),
                "people_run": people_run.to_dict(),
                "supabase": people_run.supabase,
                "budget": people_run.budget,
            },
            args.output,
        )
        return

    if args.workflow_command == "alertica-fqhc-qualify":
        pricing = _load_pricing(args.pricing_file)
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        _configure_workflow_api_audit_log(paths)
        async with HarvestClient(settings.harvest) as harvest:
            workflow = AlerticaFqhcQualifierWorkflow(harvest)
            run = await workflow.run(
                seller=args.seller,
                segment=args.segment,
                date_stamp=args.date,
                root=args.root,
                policy=AlerticaFqhcQualifierPolicy(
                    companies_input=args.companies_input,
                    people_input=args.people_input,
                    persona=args.persona,
                    exclude_people_csv=args.exclude_people_csv,
                    exclude_companies_csv=args.exclude_companies_csv,
                    exclude_supabase=args.exclude_supabase,
                    max_companies=args.max_companies,
                    max_people=args.max_people,
                    posted_limit=args.posted_limit,
                    include_company_posts=args.include_company_posts,
                    include_profile_posts=args.include_profile_posts,
                    run_company_harvest=args.run_company_harvest,
                    run_people_harvest=args.run_people_harvest,
                    run_harvest=args.run_harvest,
                ),
                pricing=pricing,
            )
        _render_json(run.to_dict(), args.output)
        return

    if args.workflow_command == "company-research":
        pricing = _load_pricing(args.pricing_file)
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        _configure_workflow_api_audit_log(paths)
        async with AsyncExitStack() as stack:
            serper = await stack.enter_async_context(SerperClient(settings.serper))
            harvest = await stack.enter_async_context(HarvestClient(settings.harvest))
            firecrawl: FirecrawlClient | None = None
            brightdata: BrightDataClient | None = None
            minimax: MiniMaxClient | None = None

            if args.crawler_provider == "firecrawl":
                firecrawl = await stack.enter_async_context(FirecrawlClient(settings.firecrawl))
            elif args.crawler_provider == "brightdata":
                if settings.brightdata is None:
                    raise ValueError(
                        "Bright Data is not configured. Add BRIGHTDATA_API_KEY and BRIGHTDATA_CRAWL_DATASET_ID to .env."
                    )
                brightdata = await stack.enter_async_context(BrightDataClient(settings.brightdata))

            if args.use_minimax:
                if settings.minimax is None:
                    raise ValueError(
                        "MiniMax is not configured. Add MINIMAX_API_KEY to .env or run with --no-use-minimax."
                    )
                minimax = await stack.enter_async_context(MiniMaxClient(settings.minimax))

            workflow = CompanyResearchWorkflow(
                serper=serper,
                harvest=harvest,
                firecrawl=firecrawl,
                brightdata=brightdata,
                minimax=minimax,
            )
            run = await workflow.run(
                seller=args.seller,
                segment=args.segment,
                date_stamp=args.date,
                root=args.root,
                policy=CompanyResearchPolicy(
                    people_csv=args.people_csv,
                    companies_csv=args.companies_csv,
                    corpus_companies_csv=args.corpus_companies_csv,
                    qualifier_only=args.qualifier_only,
                    max_companies=args.max_companies,
                    max_research_pages=args.max_research_pages,
                    include_company_posts=args.include_company_posts,
                    posted_limit=args.posted_limit,
                    research_page_timeout_ms=args.research_page_timeout_ms,
                    use_minimax=args.use_minimax,
                    crawler_provider=args.crawler_provider,
                    brightdata_domain_discovery=args.brightdata_domain_discovery,
                    seller_context=args.seller_context,
                    campaign_context=args.campaign_context,
                ),
                pricing=pricing,
            )
        _render_json(run.to_dict(), args.output)
        return

    if args.workflow_command == "campaign-prep":
        async with AsyncExitStack() as stack:
            minimax: MiniMaxClient | None = None
            if args.use_minimax_name_normalization or args.use_minimax_personalization:
                if settings.minimax is None:
                    raise ValueError(
                        "MiniMax is not configured. Add MINIMAX_API_KEY to .env or disable MiniMax personalization/name normalization."
                    )
                minimax = await stack.enter_async_context(MiniMaxClient(settings.minimax))
            workflow = CampaignPrepWorkflow(minimax=minimax)
            run = await workflow.run(
                seller=args.seller,
                segment=args.segment,
                date_stamp=args.date,
                root=args.root,
                policy=CampaignPrepPolicy(
                    people_csv=args.people_csv,
                    email_only=args.email_only,
                    use_minimax_name_normalization=args.use_minimax_name_normalization,
                    use_minimax_personalization=args.use_minimax_personalization,
                    max_people=args.max_people,
                    campaign_profile=args.campaign_profile,
                ),
            )
        _render_json(run.to_dict(), args.output)
        return

    if args.workflow_command == "campaign-review":
        workflow = CampaignReviewWorkflow()
        run = workflow.run(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
            policy=CampaignReviewPolicy(
                people_csv=args.people_csv,
                qualifier_only=args.qualifier_only,
                max_people=args.max_people,
                campaign_profile=args.campaign_profile,
            ),
        )
        _render_json(run.to_dict(), args.output)
        return

    raise ValueError(f"Unknown workflow command {args.workflow_command}")


def _run_pipeline(args: argparse.Namespace) -> None:
    if args.pipeline_command == "describe":
        template = load_pipeline_template(args.template_file)
        payload = {
            "template": template.to_dict(),
            "blueprint": [
                stage.to_dict() for stage in build_pipeline_blueprint(template)
            ],
        }
        _render_json(payload, args.output)
        return
    if args.pipeline_command == "estimate-cost":
        template = load_pipeline_template(args.template_file)
        estimate = estimate_pipeline_cost(template)
        payload = {
            "template_name": template.template_name,
            "seller": template.seller.seller_name,
            "offer": template.seller.offer_name,
            "buyer_segment": template.buyer.segment_name,
            "estimate": estimate.to_dict(),
        }
        _render_json(payload, args.output)
        return
    if args.pipeline_command == "plan-coverage":
        plan = build_coverage_plan(
            target_valid_emails=args.target_valid_emails,
            observed_companies=args.observed_companies,
            observed_people=args.observed_people,
            observed_valid_emails=args.observed_valid_emails,
            buffer_multiplier=args.buffer_multiplier,
        )
        _render_json(plan.to_dict(), args.output)
        return
    raise ValueError(f"Unknown pipeline command {args.pipeline_command}")


def _run_run_command(args: argparse.Namespace) -> None:
    if args.run_command != "scaffold":
        raise ValueError(f"Unknown run command {args.run_command}")
    paths = build_run_paths(
        seller=args.seller,
        segment=args.segment,
        date_stamp=args.date,
        root=args.root,
    )
    if args.create_dirs:
        scaffold_run_directories(paths)
    _render_json(paths.to_dict(), args.output)


def _run_dataset(args: argparse.Namespace) -> None:
    paths = build_run_paths(
        seller=args.seller,
        segment=args.segment,
        date_stamp=args.date,
    )
    if args.dataset_command == "companies-from-harvest":
        row_count = materialize_harvest_companies_to_csv(
            input_path=args.input,
            output_path=args.output,
            naming=paths.naming,
            source_query=args.source_query or "",
        )
        _render_json(
            {
                "rows_written": row_count,
                "output_path": str(args.output),
                "run_slug": paths.naming.run_slug,
            },
            None,
        )
        return
    if args.dataset_command == "people-from-harvest":
        row_count = materialize_harvest_profiles_to_csv(
            input_path=args.input,
            output_path=args.output,
            naming=paths.naming,
            company_name=args.company_name or "",
            company_domain=args.company_domain or "",
            source_role_segment=args.role_segment or "",
        )
        _render_json(
            {
                "rows_written": row_count,
                "output_path": str(args.output),
                "run_slug": paths.naming.run_slug,
            },
            None,
        )
        return
    raise ValueError(f"Unknown dataset command {args.dataset_command}")


def _run_storage(args: argparse.Namespace) -> None:
    if args.storage_command == "ping":
        ping_postgres()
        _render_json({"status": "ok"}, args.output)
        return
    if args.storage_command == "apply-schema":
        apply_schema_file(args.schema_file)
        _render_json({"schema_file": args.schema_file, "status": "applied"}, args.output)
        return
    if args.storage_command == "sync-run":
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        result = sync_run_to_supabase(
            paths=paths,
            metadata=_load_optional_json_value(
                payload_file=args.metadata_file,
                payload_json=args.metadata_json,
            ),
        )
        _render_json(
            {
                "run_slug": paths.naming.run_slug,
                **result,
            },
            args.output,
        )
        return
    if args.storage_command == "import-companies-csv":
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        result = import_companies_csv(
            input_path=args.input,
            paths=paths,
            source_query=args.source_query or "",
            upsert_supabase=args.upsert_supabase,
        )
        _render_json(result.to_dict(), args.output)
        return
    if args.storage_command == "import-people-csv":
        paths = build_run_paths(
            seller=args.seller,
            segment=args.segment,
            date_stamp=args.date,
            root=args.root,
        )
        result = import_people_csv(
            input_path=args.input,
            paths=paths,
            source_role_segment=args.role_segment or "",
            upsert_supabase=args.upsert_supabase,
        )
        _render_json(result.to_dict(), args.output)
        return
    raise ValueError(f"Unknown storage command {args.storage_command}")


async def _dispatch(args: argparse.Namespace, settings: AppSettings | None) -> None:
    if args.provider == "serper":
        if settings is None:
            raise ValueError("Provider settings are required for serper commands")
        await _run_serper(args, settings)
        return
    if args.provider == "harvest":
        if settings is None:
            raise ValueError("Provider settings are required for harvest commands")
        await _run_harvest(args, settings)
        return
    if args.provider == "million":
        if settings is None:
            raise ValueError("Provider settings are required for million commands")
        await _run_million(args, settings)
        return
    if args.provider == "prospeo":
        if settings is None:
            raise ValueError("Provider settings are required for prospeo commands")
        await _run_prospeo(args, settings)
        return
    if args.provider == "firecrawl":
        if settings is None:
            raise ValueError("Provider settings are required for firecrawl commands")
        await _run_firecrawl(args, settings)
        return
    if args.provider == "workflow":
        if settings is None:
            raise ValueError("Provider settings are required for workflow commands")
        await _run_workflow(args, settings)
        return
    if args.provider == "pipeline":
        _run_pipeline(args)
        return
    if args.provider == "run":
        _run_run_command(args)
        return
    if args.provider == "dataset":
        _run_dataset(args)
        return
    if args.provider == "storage":
        _run_storage(args)
        return
    raise ValueError(f"Unknown provider {args.provider}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="listbuild",
        description="Deterministic runner for list-building provider APIs",
    )
    provider_subparsers = parser.add_subparsers(dest="provider", required=True)

    serper = provider_subparsers.add_parser("serper", help="Run Serper API calls")
    serper_subparsers = serper.add_subparsers(dest="serper_command", required=True)
    for command_name in ("search", "news", "images", "places"):
        command = serper_subparsers.add_parser(command_name)
        command.add_argument("--query", required=True)
        command.add_argument("--gl")
        command.add_argument("--hl")
        command.add_argument("--page", type=int)
        if command_name != "places":
            command.add_argument("--num", type=int)
        if command_name == "search":
            command.add_argument(
                "--autocorrect",
                action=argparse.BooleanOptionalAction,
                default=None,
            )
        command.add_argument("--output")
    serper_raw = serper_subparsers.add_parser("raw")
    serper_raw.add_argument("--endpoint", required=True)
    serper_raw.add_argument("--payload-file")
    serper_raw.add_argument("--payload-json")
    serper_raw.add_argument("--output")

    harvest = provider_subparsers.add_parser("harvest", help="Run Harvest API calls")
    harvest_subparsers = harvest.add_subparsers(dest="harvest_command", required=True)
    harvest_company = harvest_subparsers.add_parser("company-search")
    harvest_company.add_argument("--search", required=True)
    harvest_company.add_argument("--location")
    harvest_company.add_argument("--geo-id")
    harvest_company.add_argument("--industry-id")
    harvest_company.add_argument("--company-size")
    harvest_company.add_argument("--page", type=int)
    harvest_company.add_argument("--output")
    harvest_profile = harvest_subparsers.add_parser("profile-search")
    harvest_profile.add_argument("--search", required=True)
    harvest_profile.add_argument("--current-company")
    harvest_profile.add_argument("--past-company")
    harvest_profile.add_argument("--school")
    harvest_profile.add_argument("--title")
    harvest_profile.add_argument("--location")
    harvest_profile.add_argument("--geo-id")
    harvest_profile.add_argument("--industry-id")
    harvest_profile.add_argument("--first-name")
    harvest_profile.add_argument("--last-name")
    harvest_profile.add_argument("--page", type=int)
    harvest_profile.add_argument("--output")
    harvest_geo = harvest_subparsers.add_parser("geo-id-search")
    harvest_geo.add_argument("--search", required=True)
    harvest_geo.add_argument("--output")
    harvest_raw = harvest_subparsers.add_parser("raw")
    harvest_raw.add_argument("--path", required=True)
    harvest_raw.add_argument("--payload-file")
    harvest_raw.add_argument("--payload-json")
    harvest_raw.add_argument("--output")

    million = provider_subparsers.add_parser(
        "million",
        help="Run MillionVerifier API calls",
    )
    million_subparsers = million.add_subparsers(dest="million_command", required=True)
    million_verify = million_subparsers.add_parser("verify")
    million_verify.add_argument("--email", required=True)
    million_verify.add_argument("--timeout", type=int, default=20)
    million_verify.add_argument("--output")
    million_credits = million_subparsers.add_parser("credits")
    million_credits.add_argument("--output")
    million_upload = million_subparsers.add_parser("upload")
    million_upload.add_argument("--file-path", required=True)
    million_upload.add_argument("--output")
    million_info = million_subparsers.add_parser("bulk-file-info")
    million_info.add_argument("--file-id", required=True)
    million_info.add_argument("--output")
    million_download = million_subparsers.add_parser("download-report")
    million_download.add_argument("--file-id", required=True)
    million_download.add_argument(
        "--filter-status",
        default="all",
        choices=["all", "ok", "ok_and_catch_all", "unknown", "invalid", "custom"],
    )
    million_download.add_argument("--statuses")
    million_download.add_argument(
        "--free",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    million_download.add_argument(
        "--role",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    million_download.add_argument("--output", required=True)
    million_stop = million_subparsers.add_parser("stop-bulk-file")
    million_stop.add_argument("--file-id", required=True)
    million_stop.add_argument("--output")
    million_delete = million_subparsers.add_parser("delete-bulk-file")
    million_delete.add_argument("--file-id", required=True)
    million_delete.add_argument("--output")

    prospeo = provider_subparsers.add_parser("prospeo", help="Run Prospeo API calls")
    prospeo_subparsers = prospeo.add_subparsers(dest="prospeo_command", required=True)
    prospeo_account = prospeo_subparsers.add_parser("account-information")
    prospeo_account.add_argument("--output")
    for command_name in (
        "search-person",
        "search-company",
        "enrich-person",
        "enrich-company",
    ):
        command = prospeo_subparsers.add_parser(command_name)
        command.add_argument("--payload-file")
        command.add_argument("--payload-json")
        command.add_argument("--output")
    prospeo_raw = prospeo_subparsers.add_parser("raw")
    prospeo_raw.add_argument("--method", default="POST")
    prospeo_raw.add_argument("--path", required=True)
    prospeo_raw.add_argument(
        "--category",
        choices=["search", "enrich"],
        default="search",
    )
    prospeo_raw.add_argument("--payload-file")
    prospeo_raw.add_argument("--payload-json")
    prospeo_raw.add_argument("--output")

    firecrawl = provider_subparsers.add_parser(
        "firecrawl",
        help="Run Firecrawl API calls",
    )
    firecrawl_subparsers = firecrawl.add_subparsers(
        dest="firecrawl_command",
        required=True,
    )
    firecrawl_scrape = firecrawl_subparsers.add_parser("scrape")
    firecrawl_scrape.add_argument("--url", required=True)
    firecrawl_scrape.add_argument("--format", action="append")
    firecrawl_scrape.add_argument(
        "--only-main-content",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    firecrawl_scrape.add_argument("--timeout", type=int)
    firecrawl_scrape.add_argument("--wait-for", type=int)
    firecrawl_scrape.add_argument("--max-age", type=int)
    firecrawl_scrape.add_argument("--country")
    firecrawl_scrape.add_argument("--output")
    firecrawl_map = firecrawl_subparsers.add_parser("map")
    firecrawl_map.add_argument("--url", required=True)
    firecrawl_map.add_argument("--search")
    firecrawl_map.add_argument("--limit", type=int)
    firecrawl_map.add_argument(
        "--include-subdomains",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    firecrawl_map.add_argument("--sitemap")
    firecrawl_map.add_argument("--output")
    firecrawl_search = firecrawl_subparsers.add_parser("search")
    firecrawl_search.add_argument("--query", required=True)
    firecrawl_search.add_argument("--limit", type=int)
    firecrawl_search.add_argument("--country")
    firecrawl_search.add_argument("--location")
    firecrawl_search.add_argument("--tbs")
    firecrawl_search.add_argument("--source", action="append")
    firecrawl_search.add_argument("--scrape-format", action="append")
    firecrawl_search.add_argument("--output")
    firecrawl_raw = firecrawl_subparsers.add_parser("raw")
    firecrawl_raw.add_argument("--method", default="POST")
    firecrawl_raw.add_argument("--path", required=True)
    firecrawl_raw.add_argument(
        "--category",
        choices=["scrape", "map", "search"],
        default="scrape",
    )
    firecrawl_raw.add_argument("--payload-file")
    firecrawl_raw.add_argument("--payload-json")
    firecrawl_raw.add_argument("--output")

    workflow = provider_subparsers.add_parser(
        "workflow",
        help="Run higher-level list-building workflows",
    )
    workflow_subparsers = workflow.add_subparsers(
        dest="workflow_command",
        required=True,
    )
    workflow_company = workflow_subparsers.add_parser("company-discovery")
    workflow_company.add_argument("--query", required=True)
    workflow_company.add_argument("--search-results", type=int, default=20)
    workflow_company.add_argument("--max-companies", type=int, default=10)
    workflow_company.add_argument(
        "--scrape",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company.add_argument(
        "--only-main-content",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company.add_argument("--format", action="append")
    workflow_company.add_argument("--output")

    workflow_company_scrape = workflow_subparsers.add_parser("company-scrape")
    workflow_company_scrape.add_argument("--seller", required=True)
    workflow_company_scrape.add_argument("--segment", required=True)
    workflow_company_scrape.add_argument("--query", required=True)
    workflow_company_scrape.add_argument("--date")
    workflow_company_scrape.add_argument("--root", default="runs")
    workflow_company_scrape.add_argument("--pricing-file")
    workflow_company_scrape.add_argument("--headcount-bucket", action="append")
    workflow_company_scrape.add_argument("--search-results-per-query", type=int, default=10)
    workflow_company_scrape.add_argument("--max-companies", type=int, default=100)
    workflow_company_scrape.add_argument("--max-research-pages", type=int, default=8)
    workflow_company_scrape.add_argument(
        "--crawler-provider",
        choices=("firecrawl", "brightdata"),
        default="firecrawl",
    )
    workflow_company_scrape.add_argument(
        "--include-company-posts",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company_scrape.add_argument("--posted-limit", default="month")
    workflow_company_scrape.add_argument(
        "--research-page-timeout-ms",
        type=int,
        default=60000,
    )
    workflow_company_scrape.add_argument(
        "--upsert-supabase",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_company_scrape.add_argument(
        "--dedupe-supabase",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company_scrape.add_argument("--output")

    workflow_people_scrape = workflow_subparsers.add_parser("people-scrape")
    workflow_people_scrape.add_argument("--seller", required=True)
    workflow_people_scrape.add_argument("--segment", required=True)
    workflow_people_scrape.add_argument("--companies-csv", required=True)
    workflow_people_scrape.add_argument(
        "--role-segment",
        action="append",
        required=True,
        help="Format: segment-name:title1|title2|title3",
    )
    workflow_people_scrape.add_argument("--date")
    workflow_people_scrape.add_argument("--root", default="runs")
    workflow_people_scrape.add_argument("--pricing-file")
    workflow_people_scrape.add_argument("--max-people-per-company", type=int, default=10)
    workflow_people_scrape.add_argument("--max-people-per-company-under-200", type=int)
    workflow_people_scrape.add_argument("--max-people-per-company-over-200", type=int)
    workflow_people_scrape.add_argument("--search-results-per-query", type=int, default=10)
    workflow_people_scrape.add_argument("--posted-limit", default="month")
    workflow_people_scrape.add_argument(
        "--include-email",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_people_scrape.add_argument(
        "--use-main-profile",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_people_scrape.add_argument(
        "--require-current-employer-match",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_people_scrape.add_argument(
        "--use-company-domain-in-query",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_people_scrape.add_argument(
        "--upsert-supabase",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_people_scrape.add_argument(
        "--dedupe-supabase",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_people_scrape.add_argument("--output")

    workflow_full_scrape = workflow_subparsers.add_parser("full-scrape")
    workflow_full_scrape.add_argument("--seller", required=True)
    workflow_full_scrape.add_argument("--segment", required=True)
    workflow_full_scrape.add_argument("--query", required=True)
    workflow_full_scrape.add_argument(
        "--role-segment",
        action="append",
        required=True,
        help="Format: segment-name:title1|title2|title3",
    )
    workflow_full_scrape.add_argument("--date")
    workflow_full_scrape.add_argument("--root", default="runs")
    workflow_full_scrape.add_argument("--pricing-file")
    workflow_full_scrape.add_argument("--headcount-bucket", action="append")
    workflow_full_scrape.add_argument("--search-results-per-query", type=int, default=10)
    workflow_full_scrape.add_argument("--max-companies", type=int, default=100)
    workflow_full_scrape.add_argument("--max-research-pages", type=int, default=8)
    workflow_full_scrape.add_argument(
        "--crawler-provider",
        choices=("firecrawl", "brightdata"),
        default="firecrawl",
    )
    workflow_full_scrape.add_argument(
        "--include-company-posts",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_full_scrape.add_argument("--posted-limit", default="month")
    workflow_full_scrape.add_argument(
        "--research-page-timeout-ms",
        type=int,
        default=60000,
    )
    workflow_full_scrape.add_argument("--max-people-per-company", type=int, default=10)
    workflow_full_scrape.add_argument(
        "--people-search-results-per-query",
        type=int,
        default=10,
    )
    workflow_full_scrape.add_argument("--people-posted-limit", default="month")
    workflow_full_scrape.add_argument(
        "--include-email",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_full_scrape.add_argument(
        "--use-main-profile",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_full_scrape.add_argument(
        "--upsert-supabase",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_full_scrape.add_argument(
        "--dedupe-supabase",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_full_scrape.add_argument("--output")

    workflow_alertica_fqhc_qualify = workflow_subparsers.add_parser(
        "alertica-fqhc-qualify"
    )
    workflow_alertica_fqhc_qualify.add_argument("--seller", required=True)
    workflow_alertica_fqhc_qualify.add_argument("--segment", required=True)
    workflow_alertica_fqhc_qualify.add_argument(
        "--companies-input",
        default="/Users/mortonstreet/alertica/alertica-tiers/companies_with_domains.csv",
    )
    workflow_alertica_fqhc_qualify.add_argument(
        "--people-input",
        default="/Users/mortonstreet/alertica/alertica-tiers/people_priority_ABC.csv",
    )
    workflow_alertica_fqhc_qualify.add_argument(
        "--persona",
        choices=("technical_it", "finance", "all", "leadership", "broad_fqhc", "all_current_fqhc"),
        default="technical_it",
    )
    workflow_alertica_fqhc_qualify.add_argument("--exclude-people-csv")
    workflow_alertica_fqhc_qualify.add_argument("--exclude-companies-csv")
    workflow_alertica_fqhc_qualify.add_argument(
        "--exclude-supabase",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_alertica_fqhc_qualify.add_argument("--date")
    workflow_alertica_fqhc_qualify.add_argument("--root", default="runs")
    workflow_alertica_fqhc_qualify.add_argument("--pricing-file")
    workflow_alertica_fqhc_qualify.add_argument("--max-companies", type=int, default=250)
    workflow_alertica_fqhc_qualify.add_argument("--max-people", type=int, default=1000)
    workflow_alertica_fqhc_qualify.add_argument("--posted-limit", default="month")
    workflow_alertica_fqhc_qualify.add_argument(
        "--include-company-posts",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_alertica_fqhc_qualify.add_argument(
        "--include-profile-posts",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_alertica_fqhc_qualify.add_argument(
        "--run-company-harvest",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_alertica_fqhc_qualify.add_argument(
        "--run-people-harvest",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_alertica_fqhc_qualify.add_argument(
        "--run-harvest",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_alertica_fqhc_qualify.add_argument("--output")

    workflow_company_research = workflow_subparsers.add_parser("company-research")
    workflow_company_research.add_argument("--seller", required=True)
    workflow_company_research.add_argument("--segment", required=True)
    workflow_company_research.add_argument("--people-csv")
    workflow_company_research.add_argument("--companies-csv")
    workflow_company_research.add_argument(
        "--corpus-companies-csv",
        default="/Users/mortonstreet/alertica/alertica-tiers/companies_with_domains.csv",
    )
    workflow_company_research.add_argument(
        "--qualifier-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company_research.add_argument("--date")
    workflow_company_research.add_argument("--root", default="runs")
    workflow_company_research.add_argument("--pricing-file")
    workflow_company_research.add_argument("--max-companies", type=int, default=0)
    workflow_company_research.add_argument("--max-research-pages", type=int, default=6)
    workflow_company_research.add_argument(
        "--crawler-provider",
        choices=("firecrawl", "brightdata"),
        default="firecrawl",
    )
    workflow_company_research.add_argument(
        "--brightdata-domain-discovery",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When using Bright Data, discover internal research pages from homepage, robots.txt, and sitemap seeds before scraping selected markdown pages.",
    )
    workflow_company_research.add_argument(
        "--include-company-posts",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company_research.add_argument("--posted-limit", default="month")
    workflow_company_research.add_argument(
        "--research-page-timeout-ms",
        type=int,
        default=60000,
    )
    workflow_company_research.add_argument(
        "--use-minimax",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_company_research.add_argument("--seller-context", default="")
    workflow_company_research.add_argument("--campaign-context", default="")
    workflow_company_research.add_argument("--output")

    workflow_campaign_prep = workflow_subparsers.add_parser("campaign-prep")
    workflow_campaign_prep.add_argument("--seller", required=True)
    workflow_campaign_prep.add_argument("--segment", required=True)
    workflow_campaign_prep.add_argument("--people-csv", required=True)
    workflow_campaign_prep.add_argument("--date")
    workflow_campaign_prep.add_argument("--root", default="runs")
    workflow_campaign_prep.add_argument(
        "--email-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_campaign_prep.add_argument(
        "--use-minimax-name-normalization",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_campaign_prep.add_argument(
        "--use-minimax-personalization",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    workflow_campaign_prep.add_argument(
        "--campaign-profile",
        choices=("auto", "fqhc", "msp_mssp", "generic"),
        default="auto",
    )
    workflow_campaign_prep.add_argument("--max-people", type=int, default=0)
    workflow_campaign_prep.add_argument("--output")

    workflow_campaign_review = workflow_subparsers.add_parser("campaign-review")
    workflow_campaign_review.add_argument("--seller", required=True)
    workflow_campaign_review.add_argument("--segment", required=True)
    workflow_campaign_review.add_argument("--people-csv", required=True)
    workflow_campaign_review.add_argument("--date")
    workflow_campaign_review.add_argument("--root", default="runs")
    workflow_campaign_review.add_argument(
        "--qualifier-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    workflow_campaign_review.add_argument(
        "--campaign-profile",
        choices=("auto", "fqhc", "msp_mssp", "generic"),
        default="auto",
    )
    workflow_campaign_review.add_argument("--max-people", type=int, default=0)
    workflow_campaign_review.add_argument("--output")

    pipeline = provider_subparsers.add_parser(
        "pipeline",
        help="Describe reusable GTM pipeline templates and estimate cost",
    )
    pipeline_subparsers = pipeline.add_subparsers(
        dest="pipeline_command",
        required=True,
    )
    pipeline_describe = pipeline_subparsers.add_parser("describe")
    pipeline_describe.add_argument("--template-file", required=True)
    pipeline_describe.add_argument("--output")
    pipeline_cost = pipeline_subparsers.add_parser("estimate-cost")
    pipeline_cost.add_argument("--template-file", required=True)
    pipeline_cost.add_argument("--output")
    pipeline_coverage = pipeline_subparsers.add_parser("plan-coverage")
    pipeline_coverage.add_argument("--target-valid-emails", type=int, required=True)
    pipeline_coverage.add_argument("--observed-companies", type=int, required=True)
    pipeline_coverage.add_argument("--observed-people", type=int, required=True)
    pipeline_coverage.add_argument("--observed-valid-emails", type=int, required=True)
    pipeline_coverage.add_argument("--buffer-multiplier", type=float, default=1.15)
    pipeline_coverage.add_argument("--output")

    run = provider_subparsers.add_parser(
        "run",
        help="Create deterministic run names and output paths",
    )
    run_subparsers = run.add_subparsers(dest="run_command", required=True)
    run_scaffold = run_subparsers.add_parser("scaffold")
    run_scaffold.add_argument("--seller", required=True)
    run_scaffold.add_argument("--segment", required=True)
    run_scaffold.add_argument("--date")
    run_scaffold.add_argument("--root", default="runs")
    run_scaffold.add_argument(
        "--create-dirs",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    run_scaffold.add_argument("--output")

    dataset = provider_subparsers.add_parser(
        "dataset",
        help="Materialize CSV datasets from raw provider payloads",
    )
    dataset_subparsers = dataset.add_subparsers(
        dest="dataset_command",
        required=True,
    )
    dataset_companies = dataset_subparsers.add_parser("companies-from-harvest")
    dataset_companies.add_argument("--input", required=True)
    dataset_companies.add_argument("--output", required=True)
    dataset_companies.add_argument("--seller", required=True)
    dataset_companies.add_argument("--segment", required=True)
    dataset_companies.add_argument("--date")
    dataset_companies.add_argument("--source-query")
    dataset_people = dataset_subparsers.add_parser("people-from-harvest")
    dataset_people.add_argument("--input", required=True)
    dataset_people.add_argument("--output", required=True)
    dataset_people.add_argument("--seller", required=True)
    dataset_people.add_argument("--segment", required=True)
    dataset_people.add_argument("--date")
    dataset_people.add_argument("--company-name")
    dataset_people.add_argument("--company-domain")
    dataset_people.add_argument("--role-segment")

    storage = provider_subparsers.add_parser(
        "storage",
        help="Sync deterministic run artifacts into Supabase/Postgres",
    )
    storage_subparsers = storage.add_subparsers(
        dest="storage_command",
        required=True,
    )
    storage_ping = storage_subparsers.add_parser("ping")
    storage_ping.add_argument("--output")
    storage_schema = storage_subparsers.add_parser("apply-schema")
    storage_schema.add_argument(
        "--schema-file",
        default="sql/supabase_listbuild_schema.sql",
    )
    storage_schema.add_argument("--output")
    storage_sync = storage_subparsers.add_parser("sync-run")
    storage_sync.add_argument("--seller", required=True)
    storage_sync.add_argument("--segment", required=True)
    storage_sync.add_argument("--date", required=True)
    storage_sync.add_argument("--root", default="runs")
    storage_sync.add_argument("--metadata-file")
    storage_sync.add_argument("--metadata-json")
    storage_sync.add_argument("--output")
    storage_import_companies = storage_subparsers.add_parser("import-companies-csv")
    storage_import_companies.add_argument("--input", required=True)
    storage_import_companies.add_argument("--seller", required=True)
    storage_import_companies.add_argument("--segment", required=True)
    storage_import_companies.add_argument("--date")
    storage_import_companies.add_argument("--root", default="runs")
    storage_import_companies.add_argument("--source-query")
    storage_import_companies.add_argument(
        "--upsert-supabase",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    storage_import_companies.add_argument("--output")
    storage_import_people = storage_subparsers.add_parser("import-people-csv")
    storage_import_people.add_argument("--input", required=True)
    storage_import_people.add_argument("--seller", required=True)
    storage_import_people.add_argument("--segment", required=True)
    storage_import_people.add_argument("--date")
    storage_import_people.add_argument("--root", default="runs")
    storage_import_people.add_argument("--role-segment")
    storage_import_people.add_argument(
        "--upsert-supabase",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    storage_import_people.add_argument("--output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings: AppSettings | None = None
    if args.provider in {
        "serper",
        "harvest",
        "million",
        "prospeo",
        "firecrawl",
        "workflow",
    }:
        settings = load_settings()
    elif args.provider == "storage":
        load_local_env()
    try:
        asyncio.run(_dispatch(args, settings))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
