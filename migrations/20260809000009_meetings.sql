-- Meetings as first-class contact.
--
-- Ryan records every call with Fireflies, which produces a summary, action
-- items and a full transcript. None of it has ever reached Zarvis, and the
-- first board review made the cost of that obvious: it declined to act on nine
-- of fifteen people with the reason "no correspondence on file". That was true
-- of the email record and false about the relationship. Most of those
-- conversations happened on a call.
--
-- Two constraint changes:
--
-- `channel` gains 'meeting'. It is not phone, not in_person, and not email.
--
-- `direction` gains 'mutual'. A meeting is genuinely neither inbound nor
-- outbound, and forcing it to one would corrupt the two reads that depend on
-- direction: `stale_threads()` looks for conversations where we spoke last, and
-- the board review counts unanswered outbounds. A call recorded as 'outbound'
-- would read as another unanswered message from Ryan, which is the opposite of
-- what it means.

alter table zarvis.touch drop constraint if exists touch_channel_check;
alter table zarvis.touch add constraint touch_channel_check
  check (channel = any (array[
    'email', 'whatsapp', 'linkedin', 'sms', 'phone', 'in_person', 'slack',
    'meeting'
  ]));

alter table zarvis.touch drop constraint if exists touch_direction_check;
alter table zarvis.touch add constraint touch_direction_check
  check (direction = any (array['outbound', 'inbound', 'mutual']));

-- Fireflies keeps the transcript; we keep the summary and a pointer.
--
-- Storing every full transcript would be tens of thousands of tokens per person
-- of mostly filler, and the review only ever needs the summary. The transcript
-- id makes the detail reachable on demand, which is the actual requirement:
-- context by default, deep dive when something wants one.
alter table zarvis.touch
  add column if not exists source      text,
  add column if not exists source_ref  text;

create index if not exists touch_source_idx
  on zarvis.touch (workspace_id, source, source_ref)
  where source is not null;

grant update (subject, body, source, source_ref) on zarvis.touch to zarvis_agent;
