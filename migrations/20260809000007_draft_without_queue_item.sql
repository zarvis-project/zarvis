-- Let a hand-escalated draft exist without a queue item.
--
-- `draft.queue_item_id` is NOT NULL, on the assumption that every draft answers
-- something the queue surfaced. Dana Whitfield broke that assumption on the first
-- real test: no play in the library covers a marketing partnership, so the
-- queue never surfaced him, so a draft written for him has nothing to attach to
-- and the import fails.
--
-- The tempting fix is to manufacture a queue item. That is worse than it looks.
-- Every queue_item carries a rank, a tier, an RPS and a play, and inventing
-- those to satisfy a foreign key would put fabricated numbers into the same
-- table the ranking is measured from. A draft that came from Ryan deciding to
-- act, rather than from the queue deciding to surface, genuinely has no rank.
-- The column should say so.
--
-- Reads that join through queue_item must therefore tolerate a null. The one
-- that matters is the escalation packet's "what Ryan did with previous drafts"
-- section, which already uses a left join.

alter table zarvis.draft
  alter column queue_item_id drop not null;

comment on column zarvis.draft.queue_item_id is
  'Null when the draft came from a manual escalation rather than the queue. '
  'A hand-escalated draft has no rank, tier or play, and inventing them to '
  'satisfy a constraint would corrupt the ranking data.';
