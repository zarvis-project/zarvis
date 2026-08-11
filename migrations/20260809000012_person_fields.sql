-- Structured per-person fields, with their labels and order preserved.
--
-- Written BEFORE the Notion import, deliberately. Ryan's target for the eventual
-- UI is a profile page showing "all of the Notion fields organized similar to
-- how I had them in Notion", and a CRM's field set is the shape of how someone
-- actually thinks about their book: vertical, deal size, where they came from,
-- what they sell, objections raised.
--
-- Flattening 280 pages of properties into a prose note would lose all of it.
-- The import would then have to be redone, and re-running an import that
-- created identities and touches is not a clean operation.
--
-- Why a table and not `person.attributes jsonb`:
--
--   * `position` keeps Ryan's own field ordering, which is what "organized
--     similar to how I had them" actually means.
--   * `source` records provenance. A field from Notion in 2024, a field from
--     the product today, and a field Ryan typed by hand should be
--     distinguishable, because their trustworthiness differs and the oldest
--     ones are the most likely to be stale.
--   * It is queryable. "Everyone in fintech" is a where clause rather than a
--     jsonb scan, which matters once this drives filters in a UI.
--   * A jsonb blob invites unbounded growth into the drafting prompt. A table
--     makes it explicit which fields get selected into context.

create table if not exists zarvis.person_field (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references zarvis.workspace(id) on delete cascade,
  person_id     uuid not null references zarvis.person(id) on delete cascade,

  -- The label exactly as Ryan wrote it. Not normalised: "What they sell" and
  -- "what_they_sell" are the same idea, and the one he chose is the one the
  -- profile should show.
  label         text not null,
  value         text,

  -- Notion's property type where known: rich_text, select, multi_select, date,
  -- number, url, email, phone_number, relation, checkbox. Kept so a UI can
  -- render a date as a date and a multi-select as chips.
  kind          text,

  -- Ordering as it appeared in the source, so the profile reads the way the
  -- original did.
  position      int not null default 0,

  source        text not null default 'notion',
  source_ref    text,
  observed_at   timestamptz,

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  -- One value per label per person per source. Re-importing updates in place.
  unique (workspace_id, person_id, source, label)
);

create index if not exists person_field_person_idx
  on zarvis.person_field (person_id, position);
create index if not exists person_field_label_idx
  on zarvis.person_field (workspace_id, label, value);

alter table zarvis.person_field enable row level security;

create policy zarvis_agent_all on zarvis.person_field
  for all to zarvis_agent using (true) with check (true);

grant select, insert, update on zarvis.person_field to zarvis_agent;
