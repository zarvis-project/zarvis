-- A standing inbox of "are these the same person?" questions.
--
-- The calendar harvest finds second addresses that are probably a known person
-- but not certainly. `f.nakamura.work@example.com` displays as "francis nakamura" and
-- carries 37 meetings; it is obviously Frankie Nakamura to Ryan and unprovable to a
-- matcher, because loosening the rule enough to catch it also starts merging
-- strangers who share a surname.
--
-- Printing those to a terminal was the wrong answer twice over: the suggestion
-- is lost when the scroll buffer is, and a suggestion Ryan declines comes back
-- every single night forever.
--
-- So the questions persist and the answers persist with them. A rejected match
-- is as valuable as a confirmed one, because it is the only thing that stops
-- the same wrong guess being asked again.
--
-- Shaped for a UI. The eventual screen is Google Photos' "are these the same
-- person?", one card at a time, yes / no / skip with an optional note. Nothing
-- here assumes a terminal.

create table if not exists zarvis.identity_match (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references zarvis.workspace(id) on delete cascade,

  -- The person we think this belongs to.
  person_id     uuid not null references zarvis.person(id) on delete cascade,

  -- The identity we found loose in the world.
  kind          text not null default 'email',
  value         text not null,
  display_name  text,

  -- Why the matcher thought so, and how much evidence there is. Occurrences is
  -- the tiebreaker a human actually uses: one shared meeting is a coincidence,
  -- thirty-seven is a person.
  reason        text,
  occurrences   int  not null default 1,
  first_seen    timestamptz,
  last_seen     timestamptz,

  status        text not null default 'pending'
                check (status in ('pending', 'confirmed', 'rejected')),
  -- Ryan's own words on either answer. "Yes, I call him Frankie but his real name
  -- is Franc" is worth more than the boolean, and it is the kind of fact that
  -- belongs in a draft's context later.
  note          text,
  decided_at    timestamptz,

  created_at    timestamptz not null default now(),

  -- One open question per (person, identity). Re-running the harvest updates
  -- the evidence rather than stacking duplicates.
  unique (workspace_id, person_id, kind, value)
);

create index if not exists identity_match_pending_idx
  on zarvis.identity_match (workspace_id, status, occurrences desc);

alter table zarvis.identity_match enable row level security;

create policy zarvis_agent_all on zarvis.identity_match
  for all to zarvis_agent using (true) with check (true);

-- Default privileges in this schema are SELECT-only since 20260808000004, so
-- writes are stated per table. Deliberate: forgetting is a loud error rather
-- than a silent over-grant.
grant select, insert, update on zarvis.identity_match to zarvis_agent;
