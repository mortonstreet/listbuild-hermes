# Signal-Stacking Pipeline

This repo now separates the GTM workflow into two layers:

1. transport and provider clients
2. seller/buyer-specific pipeline templates

The second layer is what makes the system reusable across offers.

## Core Principle

Every run begins by locking:

- seller point of view
- offer
- target buyer segment
- target role segments
- scoring goal

The search plan is derived from those inputs. The system should not start crawling first and decide later what matters.

## Naming And Storage

Use one run slug per scrape:

- `{seller}-{segment}-{mmddyyyy}`

Examples:

- `alertica-msps-05032026`
- `morton-street-ma-advisory-05032026`

Output tables:

- `{run_slug}-companies.csv`
- `{run_slug}-people.csv`

Raw provider payloads should not be stored as thousands of unrelated files unless a provider forces that shape.

Preferred raw format:

- one JSONL file per provider and entity family
- one row per provider response
- one run directory per scrape

Recommended layout:

- `runs/{run_slug}/raw/{run_slug}-harvest-company.jsonl`
- `runs/{run_slug}/raw/{run_slug}-harvest-profile.jsonl`
- `runs/{run_slug}/raw/{run_slug}-serper-search.jsonl`
- `runs/{run_slug}/raw/{run_slug}-firecrawl.jsonl`
- `runs/{run_slug}/outputs/{run_slug}-companies.csv`
- `runs/{run_slug}/outputs/{run_slug}-people.csv`

Recommended Supabase tables:

- `scrape_runs`
- `company_records`
- `person_records`
- `raw_provider_events`

Canonical dedupe rule:

- companies dedupe globally by LinkedIn company URL first, then domain, then company name
- people dedupe globally by LinkedIn profile URL first, then email, then company domain + full name

Why JSONL:

- append-friendly for concurrent workers
- easy to replay into CSV materializers
- easy to replay into Supabase/Postgres with deterministic event keys
- avoids filesystem overhead from tens of thousands of tiny files
- keeps lineage per provider call

## Stage Model

### 0. Offer definition

Inputs:

- seller POV
- offer thesis
- buyer segment

Outputs:

- pain hypotheses
- signal buckets
- scoring rubric

### 1. Universe discovery

Use Serper to define the company universe and recover canonical identities.

If the canonical identity already exists in Supabase/Postgres, skip the paid enrichment path and re-use the stored record.

Outputs:

- company name
- LinkedIn company URL
- discovery lineage

### 2. Company identity and org map

Use Harvest to validate the LinkedIn company and pull firm details. If Harvest does not return a usable domain, recover the official domain with Serper.

Org discovery should be role segmented, not one generic employee blob:

- economic buyer
- operator
- technical or functional stakeholder

Outputs:

- domain
- org map
- candidate LinkedIn profile URLs

### 3. People validation

Use Harvest profile JSON to validate:

- title
- tenure
- current company
- recent activity or engagement when available

Prospeo is a fallback when the primary chain misses coverage.

Outputs:

- validated people
- role evidence
- person-level signals

### 4. Company war chest

Use Firecrawl to collect first-party company context:

- website copy
- portfolio / transactions / case studies
- industries served
- press and news links

Outputs:

- first-party corpus
- specialization hypotheses

### 5. Signal branching

Branch outward from first-party evidence.

Example for Morton Street:

- if a firm focuses on industrials, search for consolidation signals in industrial distribution
- if a firm highlights healthcare services, search for reimbursement pressure or fragmented target pools

Outputs:

- macro context
- bottleneck evidence
- add-on / bolt-on difficulty clues

### 6. Scoring and export

Scoring must be explicit and reusable.

Recommended split:

- account pain score
- account fit score
- contact relevance score
- personalization readiness score

Keep scoring outputs separate from copywriting inputs so downstream messaging can be swapped independently.

## Dataset Split

`companies.csv` should contain deterministic firmographic fields first, then research fields:

- company identity: name, domain, LinkedIn URL
- deterministic enrichment: description, size, headcount
- research fields: offer, ICP, painpoint, signals
- orchestration fields: quality score, needs follow-up, source lineage

`people.csv` should contain:

- `full_name`
- `first_name`
- `last_name`
- `company_name`
- `role`
- `role_description`
- `e1`, `e2`, `e3`, `e4`
- `s1`, `s2`, `s3`, `s4`
- `p1`, `p2`, `p3`, `p4`

Interpretation:

- `role_description` is the deterministic research summary used later by copy generation
- `e*` stores four email-body spintax variants
- `s*` stores four subject-line spintax variants
- `p*` stores four personalized opener spintax variants

Hidden identity and dedupe fields like LinkedIn URL, email, company domain, Harvest IDs, and run lineage remain in raw JSONL + Supabase/Postgres, not in the outbound export CSV.

## Reusability

The same architecture works across offers because only these inputs change:

- seller POV
- buyer POV
- role segments
- signal buckets
- scoring rubric
- cost assumptions

Everything else stays structurally the same.

## Costing

The estimator in `listbuild.pipeline.costs` uses stage-level assumptions:

- universe discovery searches
- role-segmented org searches
- per-company Harvest calls
- per-person Harvest calls
- crawl pages per company
- follow-up branch searches and pages
- verification rate

You can change assumptions without changing the workflow architecture.

The example templates use explicit placeholder unit costs for:

- Harvest
- Prospeo
- LLM analysis

Those should be replaced with your real effective contract or blended spend numbers before you treat the estimate as budget-accurate.
