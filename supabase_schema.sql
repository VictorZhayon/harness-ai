-- DocAgent Harness (GitHub Edition) — Supabase schema
-- Run this in the Supabase SQL editor before starting the server.

-- HARNESS LAYER: Mistake Ledger
create table mistake_ledger (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  failure_type text,
  description text,
  correction text,
  injected_into_agents_md boolean default false
);

-- HARNESS LAYER: Observability
create table agent_runs (
  run_id uuid primary key default gen_random_uuid(),
  timestamp timestamptz default now(),
  repo_full_name text,
  files_requested jsonb,
  files_fetched jsonb,
  tools_called jsonb,
  verification_passed boolean,
  confidence_score float,
  failure_type text,
  pr_url text,
  duration_ms integer
);
