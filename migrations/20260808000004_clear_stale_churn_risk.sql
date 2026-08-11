-- Clear stale churn signals, close orphaned runs, and shut a write path into
-- product data. Verified against the live database before writing: the delete
-- matches 21 of 71 signal rows, no foreign key references zarvis.signal, and no
-- code path updates signal/touch or writes to v_user_state.
--
-- Supabase flags this as destructive because of the DELETE and the REVOKEs. Both
-- are intended. Nothing here drops a table, a column, or a schema.

begin;

-- ---------------------------------------------------------------------------
-- 1. Signals emitted by a code path that no longer exists  (deletes 21 rows)
-- ---------------------------------------------------------------------------
-- product.py originally mapped `lifecycle_former_customer` / `lifecycle_archived_at`
-- to `churn_risk` (urgency 9.5). That was wrong: those flags mean the person
-- ALREADY LEFT. It is a win-back, not a rescue. The corrected code emits
-- `churned` (urgency 3.0) instead.
--
-- These rows have to go rather than aging out on their own, because
-- `signal_dedupe_idx` is (workspace_id, source, source_ref, kind) — `kind` is in
-- the key, so the next ingest inserts 21 `churned` rows ALONGSIDE the 21 stale
-- `churn_risk` rows rather than replacing them. Urgency takes the max across live
-- signals, so 9.5 would keep beating 3.0 until these expire on 2026-08-22, and
-- the correction would look applied while changing nothing.
--
-- Safe to delete: signals are derived data, rebuilt from `zarvis.v_user_state` on
-- the next ingest. Nothing references them.
--
-- Scoped to source='supabase' deliberately. `churn_risk` remains a valid kind —
-- it is what a still-paying, visibly-deteriorating account will emit once the
-- approval-queue counts are located. Only the product-lifecycle misread is wrong.
delete from zarvis.signal
where source = 'supabase'
  and kind   = 'churn_risk';

-- ---------------------------------------------------------------------------
-- 2. Runs that died before close_run  (updates 2 rows)
-- ---------------------------------------------------------------------------
-- One dry run killed mid-flight, one real run that hit the Gmail TypeError.
-- Left as status='running' they make "is a run in progress" useless as a
-- concurrency check.
update zarvis.run
set status      = 'aborted',
    finished_at = now(),
    error       = 'orphaned: process exited before close_run'
where status = 'running'
  and finished_at is null;

-- ---------------------------------------------------------------------------
-- 3. Close a write path into product data
-- ---------------------------------------------------------------------------
-- Role setup used a blanket `grant select, insert, update on all tables in schema
-- zarvis`, and a view counts as a table for that grant. So zarvis_agent ended up
-- holding INSERT/UPDATE on `zarvis.v_user_state`.
--
-- That is not harmless. The view is a simple projection over one relation, so
-- Postgres makes it AUTO-UPDATABLE (confirmed: information_schema.views
-- .is_updatable = YES). And it is SECURITY DEFINER, so a write through it runs
-- with the view owner's privileges rather than the agent's. Net effect: an
-- `update zarvis.v_user_state set ...` would have reached `public.accts_view` and
-- the product table under it — the exact thing the read-only-against-production
-- rule exists to prevent, arrived at by way of the object built to enforce it.
--
-- Nothing in the codebase does this today. The point is that nothing should be
-- able to, including a future ingest reader written by someone who trusts the
-- docstring instead of the grants.
revoke insert, update, delete on zarvis.v_user_state from zarvis_agent;

-- Signals and touches are the audit trail the case log is built on. An agent that
-- can rewrite its own history cannot be argued with, and arguing with it is the
-- entire point of the decision log.
revoke update, delete on zarvis.signal from zarvis_agent;
revoke update, delete on zarvis.touch  from zarvis_agent;

-- ---------------------------------------------------------------------------
-- 4. Stop the hole from re-opening on its own
-- ---------------------------------------------------------------------------
-- The revokes above are not durable by themselves. There is a DEFAULT PRIVILEGE
-- on this schema — zarvis_agent=arw/postgres — that auto-grants INSERT/SELECT/
-- UPDATE on every new relation created in `zarvis`. Postgres does not distinguish
-- a view from a table there (both are objtype 'r'), so the first time
-- v_user_state is dropped and recreated — which is what an app-side schema change
-- forces — the write path comes back with no migration mentioning it.
--
-- So the default drops to SELECT, and write access becomes something a migration
-- states out loud per table.
--
-- TRADE-OFF, on purpose: every future migration that adds a zarvis table must now
-- carry its own `grant insert, update`. Forget one and the agent fails at runtime
-- with a loud `permission denied for table X`. That is the failure mode we want —
-- the alternative is a silent grant nobody asked for, which is the bug being
-- fixed here.
--
-- Does not touch existing objects; the 15 tables keep the grants they already have.
alter default privileges in schema zarvis
  revoke insert, update on tables from zarvis_agent;

commit;

-- ---------------------------------------------------------------------------
-- Verify (run separately after committing)
-- ---------------------------------------------------------------------------
-- Expect: 0 churn_risk, 0 running, and v_user_state showing SELECT only.
--
--   select kind, count(*) from zarvis.signal group by 1 order by 2 desc;
--   select status, count(*) from zarvis.run group by 1;
--   select table_name, string_agg(privilege_type, ',' order by privilege_type)
--   from information_schema.role_table_grants
--   where table_schema='zarvis' and grantee='zarvis_agent'
--     and table_name in ('v_user_state','signal','touch')
--   group by 1;
