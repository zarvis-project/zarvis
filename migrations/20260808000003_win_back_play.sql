-- Already-churned customers are a win-back, not a rescue.
begin;
update zarvis.play set trigger_kinds = array['churn_risk']
 where key = 'churn_rescue';
insert into zarvis.play
  (workspace_id, key, name, description, channel_hint, ease, energy_cost,
   base_urgency, trigger_kinds, precedence, drafts, status)
values
(null, 'win_back_churned', 'Win back a former customer',
 'They already left. No deadline, no emergency — lead with what has changed '
 'since they went, and only if something actually has. Without a changed fact '
 'this is a bump and should not fire at all.',
 'email', 5, 1, 3, array['churned'], 25, true, 'active')
on conflict (workspace_id, key) do update set
  trigger_kinds = excluded.trigger_kinds, precedence = excluded.precedence,
  base_urgency = excluded.base_urgency, updated_at = now();
commit;
