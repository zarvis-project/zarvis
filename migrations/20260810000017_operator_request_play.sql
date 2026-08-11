-- A play for "Ryan asked for this", so an on-demand draft has somewhere to live.
--
-- WHY A PLAY AT ALL
-- -----------------
-- `compose` inner-joins `play`, because a play is what says whether a move
-- produces an email at all. So a queue item with no play is invisible to the
-- composer, and asking Zarvis in Slack to write to someone who is not on the
-- board had nothing to hang the request on: it saved a note and told Ryan to
-- wait for something to surface them, which for a quiet contact is never.
--
-- The alternative was to let compose draft against a null play. That would have
-- worked and it would have been wrong: the play is also what the ledger, the
-- brief and the review read to explain WHY a person was in front of him. A
-- queue item with no play is an email with no recorded reason, and six weeks
-- later nobody can tell an on-demand request apart from a bug.
--
-- MANUAL ONLY
-- -----------
-- `trigger_kinds` is empty, which is what makes this unreachable from the
-- nightly run: `queue._pick_play` intersects a person's signal kinds with each
-- play's triggers, and an empty set never intersects. This play can therefore
-- only ever be attached by something that names it explicitly, which today is
-- exactly one caller, `slack._ensure_queue_item`.
--
-- SCORING
-- -------
-- `precedence` 100 is above every triggered play deliberately. If Ryan asks for
-- an email while a trial-expiry play is also live, the thing he asked for is
-- the move, and the trial stays visible in the evidence bundle either way.
--
-- ease 8 / energy_cost 0 / base_urgency 7 puts a requested draft near the top
-- of the board without pinning it there. It is genuinely easy (he has already
-- decided what to say) and genuinely urgent (he asked just now), and those are
-- the honest inputs rather than a thumb on the scale.

begin;

-- NOT an `on conflict (workspace_id, key)` upsert, which is what the earlier
-- play migrations use and what this originally said.
--
-- Template plays carry `workspace_id = null`, and a btree unique index treats
-- NULLs as distinct, so that clause can never fire for a shared play. The
-- statement does not error, it just INSERTS AGAIN: re-running the migration
-- would leave two `operator_request` rows and the key lookup in slack.py would
-- start returning whichever one the planner felt like. The guard below is
-- explicit about NULL instead, so this file is genuinely re-runnable.

insert into zarvis.play
  (workspace_id, key, name, description, channel_hint, ease, energy_cost,
   base_urgency, trigger_kinds, precedence, drafts, status)
select
  null, 'operator_request', 'Ryan asked for this',
  'Not a trigger. Ryan named this person in Slack and said what he wanted '
  'said, so the direction on the queue item is his, not a review''s. Treat it '
  'as settled: the decision has already been made by the only person entitled '
  'to make it, and the job is the writing.',
  'email', 8, 0, 7, '{}', 100, true, 'active'
where not exists (
  select 1 from zarvis.play
  where workspace_id is null and key = 'operator_request'
);

update zarvis.play set
  channel_hint  = 'email',
  ease          = 8,
  energy_cost   = 0,
  base_urgency  = 7,
  trigger_kinds = '{}',
  precedence    = 100,
  drafts        = true,
  status        = 'active',
  updated_at    = now()
where workspace_id is null and key = 'operator_request';

commit;
