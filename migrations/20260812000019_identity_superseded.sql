-- Retire an address without deleting it.
--
-- WHAT WENT WRONG
-- ---------------
-- An operator said, in Slack: "his email is the new one, not the old one".
-- Both addresses were on file. Every address lookup in the codebase reads
--
--     order by i.created_at limit 1
--
-- which takes the OLDEST identity, so the correction was recorded as evidence,
-- shown back to him in a receipt, and then completely ignored: the next
-- morning's draft went to the dead address again. He had already binned one
-- draft for this exact reason the day before, so the system asked him twice and
-- ignored him twice.
--
-- There was no column that could express "this address is no longer good", so
-- the correction had nowhere to live except prose in the evidence bundle, where
-- only the drafting model would ever read it, and it does not choose the
-- recipient.
--
-- WHY SUPERSEDED RATHER THAN PRIMARY
-- ----------------------------------
-- A `primary` flag invites the question "primary according to whom", needs a
-- uniqueness rule per person per kind, and has to be maintained on every insert
-- from every source. `superseded_at` states the one thing actually known: this
-- address stopped working on this date. Selection stays "oldest live one",
-- which keeps the existing stable, canonical-address behaviour and simply skips
-- the retired rows.
--
-- Nothing is deleted. A superseded address still matches incoming mail during
-- ingest, which matters: history sent to the old address is still that person's
-- history, and losing it would be a worse bug than the one this fixes.

alter table zarvis.person_identity
  add column if not exists superseded_at timestamptz,
  add column if not exists superseded_reason text;

comment on column zarvis.person_identity.superseded_at is
  'When this identity stopped being usable for OUTBOUND contact. Null means '
  'live. Superseded rows are still matched on INBOUND, because old mail is '
  'still their mail.';

create index if not exists person_identity_live_idx
  on zarvis.person_identity (person_id, kind, created_at)
  where superseded_at is null;
