-- =============================================================================
-- Plays: the first eight named moves
-- =============================================================================
-- Every one of these came out of a real case in the 2026-08-08 brain dump, not
-- a design session. See zarvis/seed/02-plays-and-calibration.md for the
-- derivation and the frequency counts.
--
-- Flat on purpose. No nodes, no edges, no call frames. `sequence_key` + `rung`
-- covers follow-up sequences: the "next rung" query picks the lowest rung with
-- no matching consumed_play row, so re-entry lands past the steps already
-- spent. That is ~90% of wormhole behaviour for ~5% of the machinery.
--
-- The statechart gets INDUCED at ~50 decision_case rows. Falsifiable tell: if
-- fewer than 3 plays are born from real skips in 30 live days, the graph
-- premise is wrong and this collapses to a rules table.

begin;

-- Which signal kinds fire this play. Kept as DATA rather than a match statement
-- in Python, so adding a play is an INSERT and not a deploy.
alter table zarvis.play
  add column if not exists trigger_kinds text[] not null default '{}';

-- Ranked when several plays apply to one person. Higher wins.
alter table zarvis.play
  add column if not exists precedence int not null default 50;

-- Some plays are never an outbound action — they belong in the brief as
-- context or as a watch item, and must never generate a draft.
alter table zarvis.play
  add column if not exists drafts boolean not null default true;

create index if not exists play_trigger_idx on zarvis.play using gin (trigger_kinds);


insert into zarvis.play
  (workspace_id, key, name, description, channel_hint,
   ease, energy_cost, base_urgency, trigger_kinds, precedence, drafts, status)
values

-- ---------------------------------------------------------------------------
-- 1. Someone said yes to meeting and it never got booked.
--    Seen in: Priya Raman. Highest-value single play in the book so far.
-- ---------------------------------------------------------------------------
(null, 'convert_stated_meeting_intent',
 'Convert a stated meeting intent',
 'They expressed willingness to meet and no calendar event exists on any day '
 'they named. Propose SPECIFIC times, never a scheduling link. Diagnostic: if '
 'both sides have already sent links and neither booked, that is a standoff '
 'and a third link loses.',
 'email', 9, 0, 9, array['meeting_intent_unconverted'], 90, true, 'active'),

-- ---------------------------------------------------------------------------
-- 2. Fresh enthusiasm decaying while setup sits unfinished.
--    Seen in: one signup, plus the five who died in the who died in the
--    tutorial. Best conversion per unit of effort available.
-- ---------------------------------------------------------------------------
(null, 'onboarding_stall_fresh',
 'Nudge a stalled onboarding while it is still warm',
 'Signup or strong verbal interest inside ~72h with onboarding incomplete. One '
 'short message naming the single blocking step. Not a checklist.',
 'email', 9, 0, 7, array['onboarding_incomplete'], 70, true, 'active'),

-- ---------------------------------------------------------------------------
-- 3. Stated intent contradicted by zero product usage.
--    Seen in: three accounts across two agencies. THREE of fifteen —
--    the dominant pattern, and it maps onto data the product already records.
-- ---------------------------------------------------------------------------
(null, 'stated_intent_zero_usage',
 'Name the gap between what they said and what they have done',
 'A verbal commitment plus zero approvals over a week or more. Usually a call, '
 'not an email — and if a deadline has already been extended once, a second '
 'extension without a commitment teaches them the deadline is not real.',
 'call', 5, 2, 6, array['zero_approvals', 'trial_state'], 80, true, 'active'),

-- ---------------------------------------------------------------------------
-- 4. The one I would not have designed. Two situations that look nothing
--    alike — a prospect who never signed up, an agency that lost clients —
--    turn out to be the same play.
-- ---------------------------------------------------------------------------
(null, 'revive_dormant_with_a_changed_fact',
 'Revive a dormant relationship with something that changed',
 'Dormant for weeks AND a fact has changed since they went cold that addresses '
 'their stated objection. Lead with the changed fact. NEVER bump. If there is '
 'no changed fact, there is no play — go find one rather than sending a '
 'check-in.',
 'email', 6, 1, 5, array['changed_fact_match', 'dormant'], 60, true, 'active'),

-- ---------------------------------------------------------------------------
-- 5. Paying, and visibly coming apart. Retaining revenue is cheaper than
--    acquiring it, so this outranks nearly every acquisition signal.
--    Seen in: Priya.
-- ---------------------------------------------------------------------------
(null, 'churn_rescue',
 'Intervene on a paying customer who is coming apart',
 'Lost sub-clients, lapsed usage, or a lifecycle flag on someone who is still '
 'paying. Lead with what has been fixed on our side, not with a check-in.',
 'whatsapp', 6, 1, 9.5, array['churn_risk'], 95, true, 'active'),

-- ---------------------------------------------------------------------------
-- 6. Calendar-driven. The action is a briefing, not an email.
--    Seen in: Nina Halvorsen, Wed 12 Aug.
-- ---------------------------------------------------------------------------
(null, 'prep_upcoming_meeting',
 'Brief before a meeting that is already booked',
 'A calendar event within 24h with a known contact. What they told you, what '
 'has changed since, what to open with. Never an outbound message — the '
 'meeting is already secured.',
 'brief_only', 8, 0, 3, array['meeting_scheduled'], 85, false, 'active'),

-- ---------------------------------------------------------------------------
-- 7. Their account is stuck and only they can unstick it.
--    Needs 2FA, payment issue, expired cookies, missing Sales Nav.
-- ---------------------------------------------------------------------------
(null, 'unblock_parked_account',
 'Unblock a parked account',
 'The account is parked and the fix requires the owner. Say exactly what is '
 'blocked and exactly what they need to do. One action, no preamble.',
 'email', 7, 0, 8, array['account_parked', 'payment_issue'], 88, true, 'active'),

-- ---------------------------------------------------------------------------
-- 8. Not an outreach play — surfaces an asset when a deal needs it.
--    Seen in: Dana/Robin. Permission is a one-time state, not a repeated
--    ask, so this shows the asset rather than requesting it.
-- ---------------------------------------------------------------------------
(null, 'harvest_proof_asset',
 'Surface an available proof asset',
 'A user has outcome numbers worth citing and an active deal is in a '
 'needs-convincing state. Show what is on the shelf; do not go ask for it '
 'again if permission already exists.',
 'brief_only', 8, 0, 4, array['proof_asset_available'], 30, false, 'active'),

-- ---------------------------------------------------------------------------
-- 9. High value, high self-sufficiency, and a known risk consciously deferred.
--    Seen in: Kai Lindstrom. Monitored, never actioned.
-- ---------------------------------------------------------------------------
(null, 'watch_only',
 'Watch, do not act',
 'High value and self-sustaining, with a latent risk Ryan has decided to leave '
 'alone. No action. Monitor the leading indicator and escalate the instant it '
 'fires, so he gets to say "I noticed" before it is a crisis.',
 'brief_only', 10, 0, 1, array['watch'], 10, false, 'active')

on conflict (workspace_id, key) do update set
  name          = excluded.name,
  description   = excluded.description,
  channel_hint  = excluded.channel_hint,
  ease          = excluded.ease,
  energy_cost   = excluded.energy_cost,
  base_urgency  = excluded.base_urgency,
  trigger_kinds = excluded.trigger_kinds,
  precedence    = excluded.precedence,
  drafts        = excluded.drafts,
  updated_at    = now();

commit;
