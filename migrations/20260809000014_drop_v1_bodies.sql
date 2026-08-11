-- Remove the superseded first extraction of Notion page bodies.
--
-- The page-body import ran twice. The first pass read only top-level blocks and
-- stored 307 signals totalling 96,336 characters. The second recursed into
-- nested blocks, mostly toggles, and stored 307 signals totalling 220,134.
--
-- **v1 is a strict subset of v2.** Same pages, same extractor, v2 simply also
-- descends into children. Keeping both means every escalation packet and every
-- deliberation carries the same notes twice, paying tokens for the duplicate
-- and inviting a model to treat one person's note as two pieces of evidence.
--
-- Deleted rather than expired: an expired signal still shows in the escalation
-- packet, which renders every signal including expired ones on purpose. There
-- is nothing here to preserve that v2 does not already contain.
--
-- Needs owner rights. `zarvis_agent` holds INSERT and SELECT on `signal` and
-- deliberately no UPDATE or DELETE, so that a bug cannot rewrite the evidence
-- the case log is built on. That protection is worth more than the convenience
-- of self-service cleanup, so this is a migration rather than a script.

delete from zarvis.signal
where source = 'notion'
  and source_ref like 'notion:body:%'
  and source_ref not like '%:v2';

-- Expect 307 deleted, and 307 v2 rows remaining:
--
--   select count(*) filter (where source_ref like '%:v2')      as v2,
--          count(*) filter (where source_ref not like '%:v2')  as v1
--   from zarvis.signal
--   where source = 'notion' and source_ref like 'notion:body:%';
