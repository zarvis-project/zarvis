-- When a draft left the mailbox, as distinct from what Ryan decided about it.
--
-- THE BUG THIS FIXES
-- ------------------
-- The verdict poller reasons like this:
--
--   gone from drafts AND a match in sent  -> sent
--   gone from drafts AND nothing in sent  -> rejected
--
-- That second line is wrong for the way Ryan actually works. He schedules mail
-- in Superhuman, and a scheduled message is held on SUPERHUMAN'S servers until
-- it fires. It is not in Sent, not in Drafts, not in Trash, and carries no
-- label. Verified by searching the whole mailbox including spam and trash: a
-- scheduled message is completely invisible to the Gmail API while pending.
--
-- Meanwhile the Zarvis draft it was composed from IS deleted the moment he
-- takes it over. So every scheduled send looked exactly like a rejection, and
-- the case log learned "Ryan binned this" about mail he had already decided to
-- send. He considers scheduled to be sent, and he is right.
--
-- WHY A TIMESTAMP RATHER THAN A NEW VERDICT VALUE
-- -----------------------------------------------
-- The situation is genuinely undetermined, not a third outcome. A scheduled
-- message lands in Sent when it fires, so simply waiting resolves it correctly
-- with no new state to interpret later. `vanished_at` records when the draft
-- left so the poller can tell "gone as of ten seconds ago" from "gone for a
-- week", and only the second one is evidence of a rejection.
--
-- Null means still in the mailbox, or already resolved.

alter table zarvis.draft
  add column if not exists vanished_at timestamptz;

comment on column zarvis.draft.vanished_at is
  'When the Gmail draft stopped existing. Set on the first poll that cannot '
  'find it. A verdict of rejected is only written once this is older than the '
  'grace period, because a scheduled send is invisible until it fires.';

-- Partial: the poller only ever asks about drafts still awaiting a verdict.
create index if not exists draft_awaiting_verdict_idx
  on zarvis.draft (workspace_id, vanished_at)
  where verdict is null and status = 'pending';
