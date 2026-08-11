-- A third path: archived.
--
-- The Notion import brings 470 contacts against the 78 people currently on
-- file. Almost all of them are networking-event contacts from two years ago,
-- and Ryan's framing is exact: "an old contact info store where 2% of the info
-- might come in useful", valuable as a rolodex and an email list, not as a
-- pipeline.
--
-- They must therefore be present and searchable without ever competing for a
-- morning slot. The existing overrides do not fit:
--
--   'dnc'     means do not contact. Wrong, and destructive: these are exactly
--             the people a future campaign would mail.
--   'custom'  means Ryan is personally driving this relationship. The opposite
--             of the truth here.
--
-- `archive` means: keep everything, rank nothing. The queue skips them, their
-- history and identities and introduction edges stay queryable, and promoting
-- one is a single column update when a reason appears.

alter table zarvis.person drop constraint if exists person_path_override_check;
alter table zarvis.person add constraint person_path_override_check
  check (path_override = any (array['custom', 'dnc', 'archive']));

comment on column zarvis.person.path_override is
  'custom = Ryan is driving this personally, skip automation. '
  'dnc = never contact. '
  'archive = keep the record, never rank it. Imported rolodex.';
