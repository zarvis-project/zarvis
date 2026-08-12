-- Make `touch` writes idempotent so history can be backfilled and re-backfilled.
--
-- Why this is needed now: `zarvis.touch` holds 3 rows for 77 people. Decay
-- pressure is computed from last touch, and with no touches it was falling back
-- to `person.created_at` — which seed.py sets to now(). Every person therefore
-- looked like they were contacted the day Zarvis was installed, decay came out
-- ~0 across the whole book, and twenty people tied at the same score.
--
-- The fix is real history from Gmail. That requires re-runnable inserts.
--
-- The key includes person_id and direction, not just external_ref: one sent
-- message addressed to three people is three touches that legitimately share a
-- Gmail message id.
create unique index if not exists touch_dedupe_idx
  on zarvis.touch (workspace_id, channel, external_ref, person_id, direction)
  where external_ref is not null;

-- `touch` is append-only for the agent (update/delete were revoked), which is
-- what we want. Inserts use ON CONFLICT DO NOTHING against the index above, and
-- that needs no extra privilege.
