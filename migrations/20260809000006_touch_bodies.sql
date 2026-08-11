-- Let touches carry the message, not just the fact that a message happened.
--
-- The Gmail backfill stored 5,775 touches as (date, direction, message id),
-- because that is all decay pressure needs. It works: decay is now real.
--
-- It is also why escalation packets are thin. Sam Okafor, rank 1 and impact
-- 10, has 14 recorded touches and 323 tokens of context, because the fourteen
-- actual emails were never read. A room convened to deliberate over that is
-- deliberating over metadata.
--
-- The bodies are still sitting in the mailbox and every touch already carries
-- the message id needed to fetch them. This adds somewhere to put them.
--
-- Why on `touch` rather than as new signals: a touch IS the message. Writing
-- them as signals would put them in the live-signal set that drives ranking,
-- where an unrecognised kind scores DEFAULT_SIGNAL_URGENCY 3.0 and every
-- hydrated person would silently acquire a floor of urgency plus a flooded
-- evidence bundle. History belongs in the history table.

alter table zarvis.touch
  add column if not exists subject text,
  add column if not exists body    text;

-- Column-level grant, deliberately narrow.
--
-- `touch` is append-only for the agent: UPDATE and DELETE were revoked so a bug
-- cannot rewrite the ledger the case log is built on. Hydration needs to fill
-- in two columns on existing rows, which is a different thing from editing
-- history, and Postgres can express exactly that.
--
-- So the agent may write the message text and may still never change when a
-- touch happened, which direction it went, or who it was with.
grant update (subject, body) on zarvis.touch to zarvis_agent;
