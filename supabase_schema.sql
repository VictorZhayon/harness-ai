-- DocAgent Harness — Supabase schema
-- Run this in the Supabase SQL editor before starting the server.

-- HARNESS LAYER: Mistake Ledger
create table if not exists mistake_ledger (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    run_id text,
    failure_type text not null check (
        failure_type in ('hallucination', 'incomplete', 'wrong_format', 'unverified_claim')
    ),
    description text not null,
    correction text not null,
    injected_into_agents_md boolean not null default false
);

-- HARNESS LAYER: Observability
create table if not exists agent_runs (
    run_id text primary key,
    timestamp timestamptz not null default now(),
    input_request text not null,
    tools_called jsonb not null default '[]'::jsonb,
    verification_passed boolean not null,
    confidence_score double precision,
    failure_type text,
    final_output text,
    duration_ms integer
);

create index if not exists idx_mistake_ledger_uninjected
    on mistake_ledger (created_at) where injected_into_agents_md = false;

create index if not exists idx_agent_runs_timestamp
    on agent_runs (timestamp desc);
