create extension if not exists pgcrypto;

create table if not exists public.scrape_runs (
  run_slug text primary key,
  seller_slug text not null,
  segment_slug text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.company_records (
  record_key text primary key,
  run_slug text not null,
  seller_slug text not null,
  segment_slug text not null,
  source_query text not null default '',
  company_name text not null default '',
  company_domain text not null default '',
  company_linkedin_url text not null default '',
  company_description text not null default '',
  company_offer text not null default '',
  company_icp text not null default '',
  company_painpoint text not null default '',
  company_size text not null default '',
  company_headcount_exact text not null default '',
  company_headcount_range text not null default '',
  company_signals text not null default '',
  company_signal_sources text not null default '',
  company_quality_score integer not null default 0,
  company_needs_followup boolean not null default true,
  harvest_company_id text not null default '',
  harvest_status text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists company_records_run_slug_idx on public.company_records (run_slug);
create index if not exists company_records_company_domain_idx on public.company_records (company_domain);

create table if not exists public.person_records (
  record_key text primary key,
  run_slug text not null,
  seller_slug text not null,
  segment_slug text not null,
  source_role_segment text not null default '',
  company_name text not null default '',
  company_domain text not null default '',
  full_name text not null default '',
  first_name text not null default '',
  last_name text not null default '',
  role text not null default '',
  role_description text not null default '',
  e1 text not null default '',
  e2 text not null default '',
  e3 text not null default '',
  e4 text not null default '',
  s1 text not null default '',
  s2 text not null default '',
  s3 text not null default '',
  s4 text not null default '',
  p1 text not null default '',
  p2 text not null default '',
  p3 text not null default '',
  p4 text not null default '',
  person_full_name text not null default '',
  person_first_name text not null default '',
  person_last_name text not null default '',
  person_linkedin_url text not null default '',
  person_title text not null default '',
  person_seniority text not null default '',
  person_location text not null default '',
  person_about text not null default '',
  person_current_company text not null default '',
  person_recent_posts_summary text not null default '',
  person_signal_summary text not null default '',
  person_email text not null default '',
  person_email_status text not null default '',
  person_quality_score integer not null default 0,
  harvest_profile_id text not null default '',
  harvest_status text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists person_records_run_slug_idx on public.person_records (run_slug);
create index if not exists person_records_company_domain_idx on public.person_records (company_domain);

alter table if exists public.person_records add column if not exists full_name text not null default '';
alter table if exists public.person_records add column if not exists first_name text not null default '';
alter table if exists public.person_records add column if not exists last_name text not null default '';
alter table if exists public.person_records add column if not exists role text not null default '';
alter table if exists public.person_records add column if not exists role_description text not null default '';
alter table if exists public.person_records add column if not exists e1 text not null default '';
alter table if exists public.person_records add column if not exists e2 text not null default '';
alter table if exists public.person_records add column if not exists e3 text not null default '';
alter table if exists public.person_records add column if not exists e4 text not null default '';
alter table if exists public.person_records add column if not exists s1 text not null default '';
alter table if exists public.person_records add column if not exists s2 text not null default '';
alter table if exists public.person_records add column if not exists s3 text not null default '';
alter table if exists public.person_records add column if not exists s4 text not null default '';
alter table if exists public.person_records add column if not exists p1 text not null default '';
alter table if exists public.person_records add column if not exists p2 text not null default '';
alter table if exists public.person_records add column if not exists p3 text not null default '';
alter table if exists public.person_records add column if not exists p4 text not null default '';

create table if not exists public.raw_provider_events (
  event_id uuid not null default gen_random_uuid(),
  event_key text not null unique,
  run_slug text not null,
  provider text not null,
  stage text not null,
  entity_type text not null default '',
  entity_key text not null default '',
  payload jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists raw_provider_events_run_slug_idx on public.raw_provider_events (run_slug);
create index if not exists raw_provider_events_provider_stage_idx on public.raw_provider_events (provider, stage);
create index if not exists raw_provider_events_entity_key_idx on public.raw_provider_events (entity_key);
