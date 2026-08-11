-- Per-call cost ledger.
--
-- Until now the only cost figure anywhere was `run.cost_usd`, written once per
-- run, and the deliberation room was not instrumented at all: its cost had to be
-- reconstructed afterwards from a markdown transcript, which missed reasoning
-- tokens entirely and could not attribute anything to a person.
--
-- The questions this table exists to answer are Ryan's:
--   what is Zarvis costing
--   where are the spikes
--   are the spikes escalations, and were those escalations recommended by the
--     review or requested by hand
--
-- None of those can be answered from a per-run total, because a run mixes a
-- board review, four routine drafts, and possibly a seven-call deep room into
-- one number. Attribution has to be per call.
--
-- `billed_ticks` is xAI's own `cost_in_usd_ticks` (1 USD = 1e10 ticks). When
-- present it is what was actually charged and beats any local price table.
-- `cost_usd` is the reconciled figure: ticks when available, the table
-- otherwise, so the column is always populated and always comparable.

create table if not exists zarvis.llm_call (
  id               uuid primary key default gen_random_uuid(),
  workspace_id     uuid not null references zarvis.workspace(id) on delete cascade,

  -- Attribution. `module` is the caller (compose, review, room, escalate);
  -- `mode` separates routine work from escalation and, within escalation,
  -- review-recommended from hand-requested.
  module           text not null,
  mode             text,
  label            text,
  person_id        uuid references zarvis.person(id) on delete set null,
  run_id           uuid references zarvis.run(id) on delete set null,

  model_id         text not null,
  input_tokens     int  not null default 0,
  cached_tokens    int  not null default 0,
  output_tokens    int  not null default 0,
  reasoning_tokens int  not null default 0,

  billed_ticks     bigint,
  cost_usd         numeric(12, 6) not null default 0,
  cost_source      text not null default 'table',  -- 'billed' | 'table'

  at               timestamptz not null default now()
);

create index if not exists llm_call_at_idx      on zarvis.llm_call (workspace_id, at desc);
create index if not exists llm_call_module_idx  on zarvis.llm_call (workspace_id, module, at desc);
create index if not exists llm_call_person_idx  on zarvis.llm_call (person_id, at desc);

alter table zarvis.llm_call enable row level security;

create policy zarvis_agent_all on zarvis.llm_call
  for all to zarvis_agent using (true) with check (true);

-- Default privileges in this schema were narrowed to SELECT in migration
-- 20260808000004, so every new table states its writes out loud. Deliberate:
-- forgetting produces a loud permission error rather than a silent over-grant.
grant select, insert on zarvis.llm_call to zarvis_agent;
