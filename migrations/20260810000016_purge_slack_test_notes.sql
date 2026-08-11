-- Remove two test notes written while building the Slack listener.
--
-- Both are fabricated. Ryan typed them to prove the round trip worked, not
-- because either is true:
--
--   93cf995c-eeb0-4d0e-9081-983e2c9d6f4d  "his budget is frozen"
--   e020367b-5d98-4fc6-82ab-1bfc04707bef  "his birthday is 8-25"
--
-- Both are attached to Sam Okafor, who is rank 1 on the board, and both
-- would otherwise land in the next review's evidence bundle. "Budget is frozen"
-- is exactly the sort of fact that would change a decision: a room that
-- believed it should not push a trial reset this week, and would be reasoning
-- from something invented.
--
-- WHY THIS IS A MIGRATION AND NOT A COMMAND
-- ------------------------------------------
-- `zarvis_agent` holds INSERT and SELECT on `signal` and deliberately neither
-- UPDATE nor DELETE, so no agent can rewrite the evidence its own decisions are
-- judged against. Slack's `undo` therefore appends a retraction rather than
-- erasing anything.
--
-- A retraction is right for a real note recorded against the wrong person. It
-- is wrong here: it would leave the invented fact AND a retraction in the
-- bundle, giving the drafting model two things to read where it should have
-- none. Test data should leave no trace, and removing it needs owner rights.
--
-- Scoped by id, not by `source = 'slack'`. Slack is now a live input and a
-- broad delete would take real notes with it the next time this file is read
-- by someone in a hurry.

delete from zarvis.signal
where id in (
  '93cf995c-eeb0-4d0e-9081-983e2c9d6f4d',
  'e020367b-5d98-4fc6-82ab-1bfc04707bef'
);

-- Expect 0 rows:
--   select s.body from zarvis.signal s
--   join zarvis.person p on p.id = s.person_id
--   where p.full_name = 'Sam Okafor' and s.source = 'slack';
