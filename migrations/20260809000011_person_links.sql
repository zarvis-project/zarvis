-- Person-to-person edges, and where the value actually lands.
--
-- Three times in two days the book has produced a relationship the schema
-- cannot express:
--
--   Lee Rankin   a dead account, impact 4, whose only reason to exist on the
--               board is that Priya Raman wants an introduction to him. Emailing
--               him is worth what the JJ opportunity is worth, not what his own
--               account is worth.
--   Dana Whitfield a partner whose value is his audience rather than his seat.
--   Okafor   "impact 10 is not one deal, it is a wedge into an org."
--
-- `zarvis.relationship` maps a person to a ROLE. `zarvis.manages` maps a
-- manager to a managed account. Neither can say "contacting A creates value for
-- B", so the ranking scores Lee Rankin as the dead account he is and never sees
-- the live opportunity hanging off him.
--
-- THE COLUMN THAT MATTERS IS `value_accrues_to`.
--
-- It is what separates this from a generic social graph. A link records not
-- just that two people are connected but WHOSE opportunity benefits from the
-- contact, which is the thing the scoring needs and the thing a room needs to
-- avoid writing a win-back email to someone Ryan has no interest in winning
-- back.

create table if not exists zarvis.link (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references zarvis.workspace(id) on delete cascade,

  from_person   uuid not null references zarvis.person(id) on delete cascade,
  to_person     uuid not null references zarvis.person(id) on delete cascade,

  -- What kind of tie. Open text with a check, because this list will grow as
  -- the book does and a rigid enum would need a migration per new shape.
  kind          text not null check (kind in (
                  'introduced',        -- from_person was introduced to to_person
                  'referral_source',   -- from_person sends business to to_person
                  'champion_for',      -- from_person advocates internally for to_person
                  'colleague',
                  'spouse',
                  'manages_account',
                  'audience_for',      -- from_person's reach serves to_person
                  'decision_maker_for'
                )),

  -- Whose opportunity gets the benefit of contacting `from_person`. Usually
  -- `to_person`, sometimes both, occasionally neither.
  value_accrues_to uuid references zarvis.person(id) on delete set null,

  -- 0..1. How much of the beneficiary's impact this link carries. A warm
  -- introduction to a decision maker is close to 1; a loose acquaintance is 0.2.
  -- Scoring reads this; without it every link would be all-or-nothing.
  strength      numeric(3,2) not null default 0.5
                check (strength >= 0 and strength <= 1),

  note          text,
  status        text not null default 'active'
                check (status in ('active', 'spent', 'dead')),

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  unique (workspace_id, from_person, to_person, kind)
);

create index if not exists link_from_idx on zarvis.link (workspace_id, from_person)
  where status = 'active';
create index if not exists link_accrues_idx on zarvis.link (value_accrues_to)
  where status = 'active';

alter table zarvis.link enable row level security;

create policy zarvis_agent_all on zarvis.link
  for all to zarvis_agent using (true) with check (true);

grant select, insert, update on zarvis.link to zarvis_agent;

comment on column zarvis.link.value_accrues_to is
  'Whose opportunity benefits from contacting from_person. This is what lets '
  'the ranking value a touch by what it is FOR rather than by who receives it.';
