-- =============================================================================
-- Zarvis v1 schema
-- =============================================================================
-- Lives in the product's Supabase project, in its own `zarvis` schema, because
-- this intelligence layer is intended to merge into Zenith rather than sit
-- beside it. See zarvis/00-charter.md, Fork 3.
--
-- Two invariants that are cheap here and expensive later:
--   1. EVERY table carries workspace_id. One nullable column now; a rewrite of
--      every query plus an ownership backfill with no ground truth if added
--      after the fact.
--   2. signal and touch are APPEND-ONLY. Enforced by revoking UPDATE/DELETE
--      from the agent role, not by convention.
--
-- Naming note: the design docs call the learning table `case`. That is a
-- reserved word in SQL, so it is `decision_case` here. Same concept.
-- =============================================================================

begin;

create schema if not exists zarvis;

create extension if not exists pgcrypto;   -- gen_random_uuid()
create extension if not exists vector;     -- pgvector. Unused in v1 on purpose:
                                           -- case similarity waits for ~50 rows.
                                           -- Enabled now so adding the column
                                           -- later is not a platform change.

-- -----------------------------------------------------------------------------
-- Shared helpers
-- -----------------------------------------------------------------------------

create or replace function zarvis.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- workspace — the tenancy root
-- -----------------------------------------------------------------------------
-- NOT the same thing as Zenith's `acct_id`, which identifies a LinkedIn-connected
-- outreach account and only exists for users who have connected one. A workspace
-- is "whose Zarvis is this" — one per Zenith customer once this ships as a
-- product feature. Prospects have no acct_id and never will; they still live in
-- a workspace.
--
-- The identifier for a human is `person_id`, on every table where a human is
-- involved. One person table covers prospects, users, agency owners, agency
-- clients, VAs and teammates alike; what differs is their `relationship` rows.
--
-- v1 has exactly one workspace (Ryan's). It exists so that workspace_id is a
-- real FK from day one rather than a loose uuid nobody validates.

create table zarvis.workspace (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  owner_email text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create trigger workspace_updated_at before update on zarvis.workspace
  for each row execute function zarvis.set_updated_at();

-- CHANGE THESE. The whole system keys off this row: ZARVIS_WORKSPACE_ID is
-- its id, and owner_email is the mailbox drafts are written into.
--   select id from zarvis.workspace;
insert into zarvis.workspace (name, owner_email)
values ('My Workspace', 'you@example.com');

-- -----------------------------------------------------------------------------
-- org — companies
-- -----------------------------------------------------------------------------
-- Discovered from the real book: half of it is org-shaped, not person-shaped.
-- A buyer plus a day-to-day user is one situation, not two unrelated facts.

create table zarvis.org (
  id         uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references zarvis.workspace(id) on delete cascade,
  name       text not null,
  domain     text,
  notes      text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger org_updated_at before update on zarvis.org
  for each row execute function zarvis.set_updated_at();

create index org_workspace_idx on zarvis.org (workspace_id);
create index org_domain_idx  on zarvis.org (workspace_id, lower(domain));

-- -----------------------------------------------------------------------------
-- person
-- -----------------------------------------------------------------------------

create table zarvis.person (
  id            uuid primary key default gen_random_uuid(),
  workspace_id    uuid not null references zarvis.workspace(id) on delete cascade,
  org_id        uuid references zarvis.org(id) on delete set null,
  full_name     text not null,
  primary_email text,
  title         text,
  timezone      text,                       -- IANA. Used to convert proposed
                                            -- meeting times into THEIR zone.
  -- Routing controls. path_override is checked FIRST, before any other rule.
  path_override text check (path_override in ('custom', 'dnc')),

  -- Deliberate silence is a state, not an absence. A prospect on vacation
  -- is "held until the 18th", not "no action needed" — and the brief shows it
  -- so Ryan stops carrying it in his head.
  suppressed_until   timestamptz,
  suppress_reason    text,
  suppress_override  text,                  -- free text: the condition that
                                            -- breaks the blackout early.

  preferred_channel  text check (preferred_channel in
                       ('email','whatsapp','linkedin','sms','phone')),
  style_notes        text,

  -- --- scoring inputs that belong to the PERSON ---------------------------
  -- Impact is a property of the human: deal size, seat count, portfolio,
  -- referral surface, how recoverable losing them is. Ease is NOT here — it
  -- belongs to the move (see zarvis.play), because replying to a warm thread
  -- and cold-calling the same person are wildly different costs.
  impact            numeric(4,2) default 5 check (impact between 1 and 10),

  -- 0..1 damper on decay pressure. A ten-seat reseller actively evangelising
  -- the product does not need attention merely because he is valuable — he
  -- needs watching, which is a different state at a different cost. Without
  -- this term, high-impact healthy relationships crowd the queue forever.
  self_sustaining   numeric(3,2) not null default 0
                      check (self_sustaining between 0 and 1),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create trigger person_updated_at before update on zarvis.person
  for each row execute function zarvis.set_updated_at();

create index person_workspace_idx    on zarvis.person (workspace_id);
create index person_org_idx        on zarvis.person (org_id);
create index person_email_idx      on zarvis.person (workspace_id, lower(primary_email));
create index person_suppressed_idx on zarvis.person (workspace_id, suppressed_until)
  where suppressed_until is not null;

-- -----------------------------------------------------------------------------
-- person_identity — the multi-channel join seam
-- -----------------------------------------------------------------------------
-- One table, costs nothing today, and it is what makes LinkedIn/WhatsApp/GHL
-- and a rented CRM joinable later without a migration. `web_session` is here
-- so an anonymous site-chat visitor can be resolved retroactively when they
-- convert.

create table zarvis.person_identity (
  id         uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references zarvis.workspace(id) on delete cascade,
  person_id  uuid not null references zarvis.person(id) on delete cascade,
  kind       text not null check (kind in
               ('email','zenith_user_id','zenith_acct_id','linkedin_urn',
                'ghl_contact_id','phone','slack_id','attio_id','web_session')),
  value      text not null,
  created_at timestamptz not null default now(),
  unique (workspace_id, kind, value)
);

create index person_identity_person_idx on zarvis.person_identity (person_id);

-- -----------------------------------------------------------------------------
-- relationship — roles as ROWS, never a column
-- -----------------------------------------------------------------------------
-- This is what lets one person be a prospect and a user and a reseller at the
-- same time without contradiction. One person can be user + reseller +
-- affiliate at once, and frequently is.

create table zarvis.relationship (
  id         uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references zarvis.workspace(id) on delete cascade,
  person_id  uuid not null references zarvis.person(id) on delete cascade,
  role       text not null check (role in
               ('prospect','user','approver','agency_owner','reseller',
                'affiliate','va','partner','team')),
  status     text not null default 'active'
               check (status in ('active','paused','former')),
  since      date,
  notes      text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (person_id, role)
);

create trigger relationship_updated_at before update on zarvis.relationship
  for each row execute function zarvis.set_updated_at();

create index relationship_person_idx on zarvis.relationship (person_id);
create index relationship_role_idx   on zarvis.relationship (workspace_id, role, status);

-- `manages` — a VA managing several accounts, or a reseller and their clients.
create table zarvis.manages (
  id           uuid primary key default gen_random_uuid(),
  workspace_id   uuid not null references zarvis.workspace(id) on delete cascade,
  manager_id   uuid not null references zarvis.person(id) on delete cascade,
  managed_id   uuid not null references zarvis.person(id) on delete cascade,
  scope        text not null check (scope in ('approvals','strategy','both','reseller')),
  created_at   timestamptz not null default now(),
  unique (manager_id, managed_id, scope),
  check (manager_id <> managed_id)
);

-- -----------------------------------------------------------------------------
-- signal — APPEND-ONLY. Every observed fact.
-- -----------------------------------------------------------------------------

create table zarvis.signal (
  id           uuid primary key default gen_random_uuid(),
  workspace_id   uuid not null references zarvis.workspace(id) on delete cascade,
  person_id    uuid references zarvis.person(id) on delete cascade,
  org_id       uuid references zarvis.org(id) on delete cascade,

  source       text not null,   -- supabase | gmail | calendar | slack | inbox_note
                                -- later: support_chat | web_chat | call_transcript
  source_ref   text,            -- 'public.daily_snapshots:41823', gmail thread id,
                                -- calendar event id. The audit trail for "why".
  kind         text not null,   -- reply_received | trial_day | zero_approvals |
                                -- list_exhausted | meeting_held | note | ...
  value        jsonb not null default '{}'::jsonb,
  body         text,            -- RAW text, never a summary. You can embed later;
                                -- you cannot un-summarize.

  -- Resolve authorship; never assume it. A Calendly "anything to prepare?"
  -- field is filled by whoever booked, which is often Ryan. Quoting his own
  -- words back to him as intel is small, corrosive and credibility-destroying.
  authored_by  text not null default 'system'
                 check (authored_by in ('system','operator','counterparty')),

  -- Context that informs ranking but must NEVER enter an evidence bundle.
  is_private   boolean not null default false,

  observed_at  timestamptz not null,
  expires_at   timestamptz,     -- TTL. Stops "congrats on the new role" 8 months late.
  created_at   timestamptz not null default now()
);

create index signal_person_idx  on zarvis.signal (person_id, observed_at desc);
create index signal_workspace_idx on zarvis.signal (workspace_id, observed_at desc);
create index signal_kind_idx    on zarvis.signal (workspace_id, kind, observed_at desc);
-- Plain composite index rather than a partial one. A predicate of
-- `expires_at > now()` is rejected: index predicates must be IMMUTABLE, and
-- now() is only STABLE — an index built against "now" would rot the instant
-- time moved. Postgres is right to refuse.
--
-- This shape still serves the hot query well; the planner filters on expiry
-- after seeking to the person.
create index signal_live_idx    on zarvis.signal (person_id, expires_at);
create unique index signal_dedupe_idx
  on zarvis.signal (workspace_id, source, source_ref, kind)
  where source_ref is not null;

-- -----------------------------------------------------------------------------
-- touch — APPEND-ONLY ledger across ALL channels. Every cap reads from here.
-- -----------------------------------------------------------------------------

create table zarvis.touch (
  id         uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references zarvis.workspace(id) on delete cascade,
  person_id  uuid not null references zarvis.person(id) on delete cascade,
  channel    text not null check (channel in
               ('email','whatsapp','linkedin','sms','phone','in_person','slack')),
  direction  text not null check (direction in ('outbound','inbound')),
  play_id    uuid,
  draft_id   uuid,
  external_ref text,           -- gmail message id, etc.
  at         timestamptz not null,
  created_at timestamptz not null default now()
);

create index touch_person_idx  on zarvis.touch (person_id, at desc);
create index touch_workspace_idx on zarvis.touch (workspace_id, at desc);

-- -----------------------------------------------------------------------------
-- play — named moves. Flat on purpose.
-- -----------------------------------------------------------------------------
-- NOT a statechart. No nodes, no edges, no call frames, no version pinning.
-- sequence_key + rung gives ~90% of wormhole behaviour for ~5% of the
-- machinery: the "next rung" query picks the lowest rung with no matching
-- consumed_play row, so re-entry lands past the steps already spent.
--
-- The full statechart gets INDUCED at ~50 decision_case rows, not authored now.
-- Falsifiable tell: <3 plays born from real skips in 30 live days means the
-- graph premise is wrong. See 00-charter.md.

create table zarvis.play (
  id            uuid primary key default gen_random_uuid(),
  -- NULL workspace_id = shared template library that accounts fork from.
  workspace_id    uuid references zarvis.workspace(id) on delete cascade,
  key           text not null,              -- 'convert_stated_meeting_intent'
  name          text not null,
  description   text,
  guard_sql     text,                       -- boolean predicate over the
                                            -- candidate view. NULL = manual only.
  prompt_key    text,                       -- file under zarvis/prompts/plays/
  channel_hint  text,                       -- email | call | whatsapp | brief_only

  -- --- scoring inputs that belong to the MOVE -----------------------------
  -- How cheap this specific touch is (1..10) and what it costs Ryan in energy
  -- (0..3). A one-line reply into a live thread is ease 9 / cost 0; a cold
  -- re-engage after six months is ease 3; a call he is dreading is cost 3.
  ease          numeric(4,2) not null default 5 check (ease between 1 and 10),
  energy_cost   numeric(4,2) not null default 0 check (energy_cost between 0 and 3),

  -- Baseline deadline pressure this play carries when its guard fires, before
  -- signal-specific escalation. NULL means "derive entirely from signals".
  base_urgency  numeric(4,2) check (base_urgency between 0 and 10),
  sequence_key  text,                       -- plays in the same sequence...
  rung          int,                        -- ...ordered by rung
  status        text not null default 'draft'
                  check (status in ('draft','active','retired')),
  support_count int not null default 0,     -- cases backing it; promote at >= 3
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (workspace_id, key)
);

create trigger play_updated_at before update on zarvis.play
  for each row execute function zarvis.set_updated_at();

create index play_active_idx   on zarvis.play (workspace_id, status) where status = 'active';
create index play_sequence_idx on zarvis.play (workspace_id, sequence_key, rung)
  where sequence_key is not null;

-- consumed_play — per-person memory of spent steps.
-- A rung burns on verdict = sent_*, NOT at queue time. A rejected draft must
-- never consume a follow-up.
create table zarvis.consumed_play (
  id          uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references zarvis.workspace(id) on delete cascade,
  person_id   uuid not null references zarvis.person(id) on delete cascade,
  play_id     uuid not null references zarvis.play(id) on delete cascade,
  scope       text not null default 'global',   -- 'global' | 'deal:<id>' | ...
  consumed_at timestamptz not null default now(),
  reason      text,
  unique (person_id, play_id, scope)
);

create index consumed_play_person_idx on zarvis.consumed_play (person_id);

-- -----------------------------------------------------------------------------
-- queue_item — the standing agenda. THE central object.
-- -----------------------------------------------------------------------------
-- A queue item is a durable proposed decision about a person. It is re-scored
-- every run, not regenerated. That distinction is the whole point:
--
--   * High-value work at position 6 does not evaporate because the cap is 5.
--     It sits in Standard and climbs as decay pressure rises.
--   * Runs stop re-deriving the same reasoning every morning. If the evidence
--     hash is unchanged, the rationale is kept and no tokens are spent.
--   * Drafting happens for the Priority tier ONLY. Score forty, draft five.
--     The cap is a limit on Ryan's attention, not on Zarvis's visibility.
--
-- Tiers are DERIVED from rank after scoring, never stored as intent:
--   rank 1-5    -> priority   (drafted, shown in full)
--   rank 6-15   -> standard   (one-liners, promotable by hand)
--   rank 16-40  -> backlog    (collapsed to a count, expandable)
--   rank 41+    -> dormant    (kept, scored, but out of sight)

create table zarvis.queue_item (
  id             uuid primary key default gen_random_uuid(),
  workspace_id     uuid not null references zarvis.workspace(id) on delete cascade,
  person_id      uuid not null references zarvis.person(id) on delete cascade,
  play_id        uuid references zarvis.play(id) on delete set null,

  -- --- scoring, kept transparent so it can be argued with -------------------
  rps            numeric(5,2),
  urgency        numeric(4,2),
  impact         numeric(4,2),
  ease           numeric(4,2),
  energy_cost    numeric(4,2),
  score_breakdown jsonb not null default '{}'::jsonb,
  deadline_pressure numeric(4,2),
  decay_pressure    numeric(4,2),

  tier           text check (tier in ('priority','standard','backlog','dormant')),
  rank           int,

  -- --- the human-facing content --------------------------------------------
  headline          text,   -- "who / why now", one line
  rationale         text,   -- the fuller why, regenerated only on evidence change
  suggested_action  text,   -- "call him" / "reply proposing Tuesday"
  channel_hint      text,
  evidence          jsonb not null default '{}'::jsonb,
  -- If this hash is unchanged since last run, the rationale is still accurate
  -- and gets reused. This is what makes "don't re-find it every morning" cheap.
  evidence_hash     text,

  -- --- lifecycle ------------------------------------------------------------
  -- 'snoozed' exists because MOST SKIPS ARE DEFERRALS, NOT REJECTIONS. Logging
  -- "not today" as a rejection would poison the learning signal with false
  -- negatives, and it is the single easiest way to corrupt the case log.
  status         text not null default 'open'
                   check (status in ('open','snoozed','actioned','dismissed',
                                     'superseded','expired')),
  snooze_until   timestamptz,
  dismiss_reason text,

  -- --- history: how long has this been sitting in front of him? ------------
  first_seen_at     timestamptz not null default now(),
  last_scored_at    timestamptz,
  scored_runs       int not null default 0,
  times_in_priority int not null default 0,
  -- An item shown in Priority eight times and never actioned is either a wrong
  -- item or an avoided task. Either way, ask about it rather than showing it a
  -- ninth time.

  actioned_at    timestamptz,
  closed_at      timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create trigger queue_item_updated_at before update on zarvis.queue_item
  for each row execute function zarvis.set_updated_at();

-- One live item per person+play. This is the persistent idempotency mechanism:
-- a second run cannot create a duplicate agenda entry for the same situation.
create unique index queue_item_live_idx
  on zarvis.queue_item (workspace_id, person_id, play_id)
  where status in ('open', 'snoozed');

create index queue_item_rank_idx on zarvis.queue_item (workspace_id, tier, rank)
  where status = 'open';
create index queue_item_person_idx on zarvis.queue_item (person_id, created_at desc);
create index queue_item_stuck_idx on zarvis.queue_item (workspace_id, times_in_priority desc)
  where status = 'open';
create index queue_item_snooze_idx on zarvis.queue_item (workspace_id, snooze_until)
  where status = 'snoozed';

-- -----------------------------------------------------------------------------
-- draft — composed copy for a queue item. Priority tier only.
-- -----------------------------------------------------------------------------

create table zarvis.draft (
  id              uuid primary key default gen_random_uuid(),
  workspace_id      uuid not null references zarvis.workspace(id) on delete cascade,
  queue_item_id   uuid not null references zarvis.queue_item(id) on delete cascade,
  person_id       uuid not null references zarvis.person(id) on delete cascade,
  run_id          uuid,

  -- Re-running the cron is a no-op, not a double-queue.
  idempotency_key text not null,

  channel         text not null default 'email',
  evidence_hash   text,      -- copy is stale if this drifts from the queue item

  subject         text,
  proposed_body   text not null,

  -- Provenance: makes "why did you decide this?" answerable, and makes a bad
  -- template killable by class rather than by killing the agent.
  model_id        text,
  prompt_version  text,

  status          text not null default 'pending'
                    check (status in ('pending','approved','skipped','expired')),
  skip_reason     text,                      -- captured in Slack on skip.
                                             -- THE most valuable byte here.

  gmail_draft_id  text,                      -- draft id, NOT message id:
                                             -- editing mints a new message id
                                             -- while the draft id persists.

  -- Verdict, filled by the next run's poller.
  verdict         text check (verdict in
                    ('sent_unedited','sent_edited','rejected','expired','unclassified')),
  final_body      text,
  edit_distance   numeric(5,4),
  verdict_at      timestamptz,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (workspace_id, idempotency_key)
);

create trigger draft_updated_at before update on zarvis.draft
  for each row execute function zarvis.set_updated_at();

create index draft_pending_idx    on zarvis.draft (workspace_id, status, created_at desc);
create index draft_person_idx     on zarvis.draft (person_id, created_at desc);
create index draft_queue_item_idx on zarvis.draft (queue_item_id, created_at desc);
create index draft_gmail_idx      on zarvis.draft (gmail_draft_id)
  where gmail_draft_id is not null;

-- -----------------------------------------------------------------------------
-- decision_case — the learning substrate (docs call this `case`)
-- -----------------------------------------------------------------------------
-- This table IS the training set the graph gets induced from, and it is
-- worthless if it starts three weeks late. It is the reason the case log
-- exists in markdown today.

create table zarvis.decision_case (
  id                uuid primary key default gen_random_uuid(),
  workspace_id        uuid not null references zarvis.workspace(id) on delete cascade,
  person_id         uuid references zarvis.person(id) on delete set null,
  queue_item_id     uuid references zarvis.queue_item(id) on delete set null,
  draft_id          uuid references zarvis.draft(id) on delete set null,
  play_offered      uuid references zarvis.play(id) on delete set null,

  features          jsonb not null default '{}'::jsonb,
  situation         text,
  options           jsonb,
  chosen            text,
  rationale         text,
  outside_variables jsonb,

  verdict           text,
  reason_code       text,   -- routes the learning:
                            --   different_situation | wrong_ask -> the tree
                            --   tone | facts_wrong   -> style card + linter ONLY
                            --   already_handled      -> neither. Excluded.
  proposed_body     text,
  final_body        text,
  edit_distance     numeric(5,4),

  outcome           jsonb,  -- backfilled ~14d. Without this, retention is hoarding.
  outcome_at        timestamptz,

  -- pgvector column deliberately omitted in v1. Add at ~50 rows:
  --   alter table zarvis.decision_case add column embedding vector(1536);
  --   create index on zarvis.decision_case using hnsw (embedding vector_cosine_ops);

  created_at        timestamptz not null default now()
);

create index decision_case_person_idx on zarvis.decision_case (person_id, created_at desc);
create index decision_case_play_idx   on zarvis.decision_case (play_offered, reason_code);

-- -----------------------------------------------------------------------------
-- run — observability. Distinguishes "ran and decided nothing" from "didn't run".
-- -----------------------------------------------------------------------------
-- Alert on ZERO CANDIDATES, not zero output. A silent no-op is the failure
-- mode that kills cron agents quietly.

create table zarvis.run (
  id             uuid primary key default gen_random_uuid(),
  workspace_id     uuid not null references zarvis.workspace(id) on delete cascade,
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  status         text not null default 'running'
                   check (status in ('running','completed','failed','aborted')),
  dry_run        boolean not null default false,
  signals_seen   int,
  candidates     int,
  drafted        int,        -- priority tier only; scoring is free, drafting is not
  queue          jsonb,      -- {priority, standard, backlog, dormant, new, closed}
  suppressed     jsonb,      -- {reason: count} — why people did NOT make the cut
  cost_usd       numeric(8,4),
  error          text
);

create index run_workspace_idx on zarvis.run (workspace_id, started_at desc);

-- -----------------------------------------------------------------------------
-- cursor — incremental sync state per source
-- -----------------------------------------------------------------------------
-- Gmail history ids, Calendar sync tokens. Small, but it is what turns ingest
-- from "re-scan everything" into an incremental read (history.list costs 2
-- quota units against a 6,000/min budget).

create table zarvis.cursor (
  workspace_id uuid not null references zarvis.workspace(id) on delete cascade,
  source     text not null,
  key        text not null default 'default',
  value      text not null,
  updated_at timestamptz not null default now(),
  primary key (workspace_id, source, key)
);


-- -----------------------------------------------------------------------------
-- Row level security
-- -----------------------------------------------------------------------------
-- v1 has one workspace, so this is not load-bearing yet. It exists so that
-- exposing Zarvis to Zenith customers later is a configuration change rather
-- than an audit of every query.
--
-- ‼️  RLS WITH NO POLICIES DENIES EVERYTHING to any role lacking BYPASSRLS.
--     `zarvis_agent` is a plain login role, so until the policies in the role
--     setup block (bottom of this file) exist, every SELECT it runs returns
--     zero rows and every INSERT fails.
--
--     Reads fail SILENTLY — an empty result set, not an error. That is exactly
--     the shape of bug that reads as "ingest found nothing today" and takes an
--     afternoon to find. The policies cannot live in this file because the role
--     does not exist yet; create them in the same step that creates the role.
--
-- Table owners bypass RLS by default, so whoever applies this migration keeps
-- full access regardless, as does Supabase's service_role.
--
-- Written out one statement per table rather than as a loop, deliberately:
-- Supabase's SQL editor lints `create table` statically and cannot see inside a
-- `do $$ ... $$` block, so a loop makes it warn that these tables have no RLS
-- when they do. Explicit statements keep the linter quiet and honest.
-- If you add a table above, add it here too.

alter table zarvis.workspace       enable row level security;
alter table zarvis.org             enable row level security;
alter table zarvis.person          enable row level security;
alter table zarvis.person_identity enable row level security;
alter table zarvis.relationship    enable row level security;
alter table zarvis.manages         enable row level security;
alter table zarvis.signal          enable row level security;
alter table zarvis.touch           enable row level security;
alter table zarvis.play            enable row level security;
alter table zarvis.consumed_play   enable row level security;
alter table zarvis.queue_item      enable row level security;
alter table zarvis.draft           enable row level security;
alter table zarvis.decision_case   enable row level security;
alter table zarvis.run             enable row level security;
alter table zarvis.cursor          enable row level security;

-- -----------------------------------------------------------------------------
-- Append-only enforcement
-- -----------------------------------------------------------------------------
-- Convention is not enforcement. The agent role gets INSERT and SELECT on the
-- ledgers and nothing else, so a bug cannot rewrite history.
--
-- Run this AFTER the migration commits, as a privileged user. It is reproduced
-- in zarvis/SETUP.md with the reasoning.
--
--   create role zarvis_agent login password 'GENERATE_ONE';
--
--   grant usage on schema zarvis to zarvis_agent;
--   grant select, insert, update on all tables in schema zarvis to zarvis_agent;
--   alter default privileges in schema zarvis
--     grant select, insert, update on tables to zarvis_agent;
--
--   -- append-only ledgers: history cannot be rewritten, even by a bug
--   revoke update on zarvis.signal, zarvis.touch from zarvis_agent;
--   revoke delete on all tables in schema zarvis from zarvis_agent;
--
--   -- product access is ONE view and nothing else. No grant on public.users,
--   -- no grant on any product table.
--   grant select on zarvis.v_user_state to zarvis_agent;
--
--   alter role zarvis_agent set statement_timeout = '30s';
--
--   -- REQUIRED. RLS is enabled above with no policies, which denies everything.
--   -- Without this loop the agent reads zero rows and writes nothing, and the
--   -- reads fail silently. `using (true)` is correct for a single-workspace
--   -- deployment; when a second workspace exists, replace it with
--   --   workspace_id = current_setting('zarvis.workspace_id')::uuid
--   -- and have the agent SET that on connect.
--   do $$
--   declare t text;
--   begin
--     foreach t in array array[
--       'workspace','org','person','person_identity','relationship','manages',
--       'signal','touch','play','consumed_play','queue_item','draft',
--       'decision_case','run','cursor'
--     ]
--     loop
--       execute format(
--         'create policy zarvis_agent_all on zarvis.%I
--            for all to zarvis_agent using (true) with check (true)', t);
--     end loop;
--   end $$;
--
-- The agent role must NOT hold write access to any public.* table, and should
-- not hold direct read access either. Ingest reaches the product through the
-- view or not at all.

commit;
