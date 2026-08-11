"""Import the historical Notion CRM: contacts, fields, notes, introductions.

    PYTHONPATH=src python -m zarvis.notion --dry-run
    PYTHONPATH=src python -m zarvis.notion --apply

WHAT THIS IS FOR
----------------
Zarvis remembers nothing before 2025-02-18. The Gmail backfill reaches twelve
months and Fireflies reaches twelve months; the years in which these
relationships were actually formed are invisible. The Notion CRM is the only
record of them: 470 contacts, 411 with an email address, 375 with a hand-written
last-touch date.

IMPORTED AS ARCHIVE, DELIBERATELY
----------------------------------
470 contacts against 78 people on file. Ryan's framing: "an old contact info
store where 2% of the info might come in useful", a rolodex and a future mailing
list, not a pipeline. So every newly created person gets `impact = 1` and
`path_override = 'archive'`, which keeps them searchable, keeps their history and
their introduction edges live, and keeps them out of the morning queue forever
until something promotes them.

**People already on file are never downgraded.** Priya Raman is in this database
too, and importing must not overwrite his impact with 1 or archive him. The
import adds to known people and archives only the ones it creates.

THE INTRODUCTION GRAPH IS THE REAL PRIZE
-----------------------------------------
`Introduced By`, `Introduced To` and `I Introduced Them` hold 248 relation edges.
That is the thing the schema could not express two hours ago and that Ryan
described from memory with Lee Rankin and Priya Raman. It was written down all
along. Those become `zarvis.link` rows with `value_accrues_to` set, which is
what lets a touch be valued by what it is FOR rather than by who receives it.

WHAT IS NOT IMPORTED
--------------------
`Pipeline`, `Intent` and `Temp` land in `person_field` as profile context and do
NOT influence ranking. Ryan's call, and the right one: this data is two years
stale and a stale "hot" flag would distort a live queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

import psycopg

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.notion")

CONTACTS_DB = "850b1fcd-c168-4fe7-b3c6-061236abfc6f"
OPPORTUNITIES_DB = "2595ec50-ef77-8069-94a5-ca3a87791e7f"

# Straight to a person column or identity rather than a profile field.
#
# LinkedIn is deliberately NOT here. The only LinkedIn identity kind the schema
# allows is `linkedin_urn`, and what Notion holds is a profile URL. Storing a URL
# in a URN slot is the kind of small type lie that costs an afternoon when
# something later tries to resolve a person by URN and matches nothing. It goes
# to `person_field` with the rest of the profile instead.
IDENTITY_PROPS = {"Email": "email", "Phone": "phone"}
# Prose that belongs in a note signal, not a field chip.
NOTE_PROPS = ("Quick Note", "Action Item", "Next Touch Task")
# Dates that evidence a real conversation.
TOUCH_DATE_PROPS = ("Last Meeting (manual)", "Last Touch (manual)")
# The introduction graph.
RELATION_PROPS = {
    "Introduced By": "introduced",
    "Introduced To": "introduced",
    "I Introduced Them": "introduced",
}
# Not imported.
#
# `Last Email` and `Last Meeting` are ROLLUPS, and Ryan's correction is decisive:
# they were populated by his own earlier imports of Gmail and Fireflies data INTO
# Notion, and have not updated since. So they are a stale second-hand copy of two
# sources Zarvis now reads directly, completely, and continuously.
#
# The danger is not that they are old, it is that their labels claim to be
# current. A profile showing "Last Email: 2024-06-01" beside a touch table that
# reaches today invites exactly the wrong conclusion, and a room reasoning about
# dormancy would take the earlier date as evidence. Derived data is only worth
# importing when it is better than what you can derive yourself, and here it is
# strictly worse.
#
# `Last Touch` (formula) is skipped for the same reason. The `(manual)` date
# properties are kept, because Ryan typed those himself at the time.
SKIP_PROPS = {
    "Name", "Added", "Update Email Send Status",
    "Last Touch", "Last Email", "Last Meeting",
}


def _call(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    token = os.environ.get("ZARVIS_NOTION_TOKEN")
    if not token:
        raise RuntimeError("ZARVIS_NOTION_TOKEN is not set")
    req = urllib.request.Request(
        "https://api.notion.com/v1/" + path,
        data=json.dumps(payload).encode() if payload else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "content-type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code}: {exc.read().decode()[:300]}") from exc


def _text(prop: dict) -> str:
    """One property, flattened to something a human would recognise."""
    kind = prop.get("type")
    if kind in ("rich_text", "title"):
        return "".join(x.get("plain_text", "") for x in prop.get(kind) or [])
    if kind == "email":
        return prop.get("email") or ""
    if kind == "phone_number":
        return prop.get("phone_number") or ""
    if kind == "url":
        return prop.get("url") or ""
    if kind == "select":
        return ((prop.get("select") or {}).get("name")) or ""
    if kind == "status":
        return ((prop.get("status") or {}).get("name")) or ""
    if kind == "multi_select":
        return ", ".join(x["name"] for x in prop.get("multi_select") or [])
    if kind == "date":
        return ((prop.get("date") or {}).get("start")) or ""
    if kind == "checkbox":
        return "yes" if prop.get("checkbox") else ""
    if kind == "number":
        n = prop.get("number")
        return "" if n is None else str(n)
    if kind == "created_time":
        return prop.get("created_time") or ""
    if kind == "formula":
        f = prop.get("formula") or {}
        return str(f.get("string") or (f.get("date") or {}).get("start") or f.get("number") or "")
    if kind == "rollup":
        # Found by audit, not by reading the schema: `Last Email (rollup)` and
        # `Last Meeting (rollup)` are populated on all 470 rows and did not
        # appear in the database schema endpoint at all. Returning "" for them
        # dropped 940 dated values silently.
        r = prop.get("rollup") or {}
        if r.get("type") == "date":
            return ((r.get("date") or {}).get("start")) or ""
        if r.get("type") == "number":
            n = r.get("number")
            return "" if n is None else str(n)
        if r.get("type") == "array":
            return ", ".join(filter(None, (_text(x) for x in r.get("array") or [])))
        return ""
    return ""


def _rows() -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _call(f"databases/{CONTACTS_DB}/query", "POST", body)
        out.extend(res["results"])
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return out


def _existing(conn: psycopg.Connection) -> tuple[dict, dict]:
    ws = get_config().workspace_id
    with conn.cursor() as cur:
        cur.execute(
            "select person_id, lower(value) v from zarvis.person_identity "
            "where workspace_id = %s and kind = 'email'",
            (ws,),
        )
        by_email = {r["v"]: str(r["person_id"]) for r in cur.fetchall()}
        cur.execute(
            "select id, lower(full_name) n from zarvis.person where workspace_id = %s",
            (ws,),
        )
        by_name = {r["n"]: str(r["id"]) for r in cur.fetchall()}
    return by_email, by_name


def run(conn: psycopg.Connection, *, apply: bool) -> dict:
    ws = get_config().workspace_id
    rows = _rows()
    by_email, by_name = _existing(conn)
    schema_order = list(_call(f"databases/{CONTACTS_DB}")["properties"].keys())

    counts = {
        "contacts": len(rows), "created": 0, "matched": 0, "skipped_no_name": 0,
        "identities": 0, "fields": 0, "notes": 0, "touches": 0, "links": 0,
    }
    notion_to_person: dict[str, str] = {}
    pending_links: list[tuple[str, str, str]] = []  # notion_from, notion_to, kind

    for row in rows:
        props = row["properties"]
        name = _text(props.get("Name", {})).strip()
        if not name:
            counts["skipped_no_name"] += 1
            continue
        email = _text(props.get("Email", {})).strip().lower()

        person_id = by_email.get(email) if email else None
        if not person_id:
            person_id = by_name.get(name.lower())
        is_new = person_id is None

        if is_new:
            counts["created"] += 1
            if apply:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into zarvis.person
                            (workspace_id, full_name, primary_email, title, impact,
                             path_override)
                        values (%s, %s, %s, %s, 1, 'archive')
                        returning id
                        """,
                        (ws, name, email or None,
                         _text(props.get("Title", {})).strip() or None),
                    )
                    person_id = str(cur.fetchone()["id"])
                conn.commit()
            else:
                person_id = f"new:{row['id']}"
        else:
            # Known person: enrich, never downgrade. Their impact and path were
            # set by Ryan's judgment overlay and this data is two years old.
            counts["matched"] += 1

        notion_to_person[row["id"]] = person_id

        for prop_name, kind in IDENTITY_PROPS.items():
            value = _text(props.get(prop_name, {})).strip()
            if not value:
                continue
            counts["identities"] += 1
            if apply and not person_id.startswith("new:"):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into zarvis.person_identity
                            (workspace_id, person_id, kind, value)
                        values (%s, %s, %s, %s)
                        on conflict (workspace_id, kind, value) do nothing
                        """,
                        (ws, person_id, kind, value.lower() if kind == "email" else value),
                    )
                conn.commit()

        for prop_name, prop in props.items():
            if prop_name in SKIP_PROPS or prop_name in IDENTITY_PROPS:
                continue
            if prop_name in NOTE_PROPS or prop_name in RELATION_PROPS:
                continue
            value = _text(prop).strip()
            if not value:
                continue
            counts["fields"] += 1
            if apply and not person_id.startswith("new:"):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into zarvis.person_field
                            (workspace_id, person_id, label, value, kind, position,
                             source, source_ref)
                        values (%s, %s, %s, %s, %s, %s, 'notion', %s)
                        on conflict (workspace_id, person_id, source, label)
                        do update set value = excluded.value,
                                      updated_at = now()
                        """,
                        (ws, person_id, prop_name, value, prop.get("type"),
                         schema_order.index(prop_name) if prop_name in schema_order else 99,
                         row["id"]),
                    )
                conn.commit()

        note = "\n\n".join(
            f"{p}: {_text(props.get(p, {})).strip()}"
            for p in NOTE_PROPS
            if _text(props.get(p, {})).strip()
        )
        if note:
            counts["notes"] += 1
            if apply and not person_id.startswith("new:"):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into zarvis.signal
                            (workspace_id, person_id, source, source_ref, kind,
                             observed_at, body, authored_by)
                        values (%s, %s, 'notion', %s, 'note', %s, %s, 'operator')
                        -- DO NOTHING, not DO UPDATE. `signal` is append-only
                        -- for the agent: UPDATE was revoked so a bug cannot
                        -- rewrite the evidence the case log is built on, and an
                        -- import is not a good enough reason to make an
                        -- exception. Re-running is therefore a no-op rather
                        -- than a rewrite.
                        on conflict do nothing
                        """,
                        (ws, person_id, f"notion:{row['id']}",
                         row.get("created_time") or datetime.now(UTC), note),
                    )
                conn.commit()

        # A recorded date is evidence of a conversation. Ryan wrote these by
        # hand at the time, which makes them a fact rather than the inference
        # we discussed falling back on.
        for prop_name in TOUCH_DATE_PROPS:
            when = _text(props.get(prop_name, {})).strip()
            if not when:
                continue
            counts["touches"] += 1
            if apply and not person_id.startswith("new:"):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into zarvis.touch
                            (workspace_id, person_id, channel, direction,
                             external_ref, at, subject, body, source, source_ref)
                        values (%s, %s, 'meeting', 'mutual', %s, %s, %s, %s,
                                'notion', %s)
                        on conflict (workspace_id, channel, external_ref, person_id,
                                     direction) where external_ref is not null
                        do update set subject = excluded.subject,
                                      body = excluded.body
                        """,
                        (ws, person_id, f"notion:{row['id']}:{prop_name}", when,
                         f"Notion CRM: {prop_name}",
                         note or f"Recorded in the Notion CRM under '{prop_name}'. "
                                 f"Date is Ryan's own entry, not a confirmed calendar event.",
                         row["id"]),
                    )
                conn.commit()

        for prop_name, kind in RELATION_PROPS.items():
            for rel in (props.get(prop_name, {}).get("relation") or []):
                pending_links.append((row["id"], rel["id"], prop_name))

    # Links last: both ends must exist before an edge can point at them.
    for from_notion, to_notion, prop_name in pending_links:
        a = notion_to_person.get(from_notion)
        b = notion_to_person.get(to_notion)
        if not a or not b or a == b:
            continue
        counts["links"] += 1
        if not apply or a.startswith("new:") or b.startswith("new:"):
            continue
        # "Introduced By" points at whoever made the introduction; the value of
        # the resulting relationship accrues to the person on this row.
        if prop_name == "Introduced By":
            from_id, to_id, accrues = b, a, a
        else:
            from_id, to_id, accrues = a, b, b
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into zarvis.link
                    (workspace_id, from_person, to_person, kind, value_accrues_to,
                     strength, note, status)
                values (%s, %s, %s, 'introduced', %s, 0.5, %s, 'active')
                on conflict (workspace_id, from_person, to_person, kind) do nothing
                """,
                (ws, from_id, to_id, accrues, f"From the Notion CRM: {prop_name}"),
            )
        conn.commit()

    return counts



def _blocks_text(page_id: str, depth: int = 0) -> str:
    """Page body, flattened. Rare but irreplaceable.

    Only about one page in twenty-five has any body content, which on an
    incremental sync would be a rounding error. This is a one-way extraction
    before the Notion token is revoked, so a fact that exists nowhere else is
    worth 470 requests to collect. One sampled page held two product URLs that
    appear in no property.
    """
    out: list[str] = []
    cursor = None
    while True:
        path = f"blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        try:
            res = _call(path)
        except RuntimeError:
            return ""
        for b in res.get("results", []):
            kind = b.get("type")
            data = b.get(kind) or {}
            text = "".join(x.get("plain_text", "") for x in (data.get("rich_text") or []))
            if text.strip():
                out.append(text)
            # Recurse. Toggles, nested lists and callouts hold their content in
            # CHILD blocks, and reading only the top level returns the toggle's
            # label while silently discarding everything folded inside it, which
            # is precisely where a person files the detail worth keeping. An
            # audit found 6 of 124 sampled blocks had unread children.
            if b.get("has_children") and depth < 3:
                child = _blocks_text(b["id"], depth + 1)
                if child.strip():
                    out.append(child)
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return "\n".join(out)


def import_bodies(conn: psycopg.Connection, *, apply: bool) -> dict:
    ws = get_config().workspace_id
    counts = {"pages": 0, "with_body": 0, "written": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct on (person_id) person_id, source_ref
            from zarvis.person_field
            where workspace_id = %s and source = 'notion' and source_ref is not null
            """,
            (ws,),
        )
        pages = cur.fetchall()

    for row in pages:
        counts["pages"] += 1
        text = _blocks_text(row["source_ref"])
        if not text.strip():
            continue
        counts["with_body"] += 1
        if not apply:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into zarvis.signal
                    (workspace_id, person_id, source, source_ref, kind,
                     observed_at, body, authored_by)
                values (%s, %s, 'notion', %s, 'note', now(), %s, 'operator')
                on conflict do nothing
                """,
                # Versioned. `signal` is append-only, so re-extracting the same
                # page with a better parser collides with the old row and is
                # discarded. Bump this whenever the extractor changes what it
                # can see; v2 is the pass that recurses into toggles.
                (ws, row["person_id"], f"notion:body:{row['source_ref']}:v2",
                 "From the Notion page body:\n\n" + text[:8000]),
            )
        conn.commit()
        counts["written"] += 1
    return counts


def import_opportunities(conn: psycopg.Connection, *, apply: bool) -> dict:
    """The Opportunity Pipe, attached to its primary contacts.

    Stale by Ryan's own account, so it lands as profile context rather than
    anything the ranking reads. Deal stage and value on a two-year-old row are
    history, not pipeline.
    """
    ws = get_config().workspace_id
    counts = {"opportunities": 0, "attached": 0, "orphans": 0}

    with conn.cursor() as cur:
        cur.execute(
            "select person_id, source_ref from zarvis.person_field "
            "where workspace_id = %s and source = 'notion' and source_ref is not null",
            (ws,),
        )
        page_to_person = {r["source_ref"]: str(r["person_id"]) for r in cur.fetchall()}

    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _call(f"databases/{OPPORTUNITIES_DB}/query", "POST", body)
        for row in res["results"]:
            counts["opportunities"] += 1
            props = row["properties"]
            name = _text(props.get("Name", {})).strip() or "(untitled)"
            contacts = [
                page_to_person[r["id"]]
                for r in (props.get("Primary Contant(s)", {}).get("relation") or [])
                if r["id"] in page_to_person
            ]
            if not contacts:
                counts["orphans"] += 1
                continue

            summary = "\n".join(
                f"{k}: {_text(v).strip()}"
                for k, v in props.items()
                if k != "Name" and _text(v).strip()
            )
            for person_id in contacts:
                counts["attached"] += 1
                if not apply:
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into zarvis.person_field
                            (workspace_id, person_id, label, value, kind, position,
                             source, source_ref)
                        values (%s, %s, %s, %s, 'rich_text', 200, 'notion', %s)
                        on conflict (workspace_id, person_id, source, label)
                        do update set value = excluded.value, updated_at = now()
                        """,
                        (ws, person_id, f"Opportunity: {name}"[:120], summary,
                         row["id"]),
                    )
                conn.commit()
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bodies", action="store_true",
                        help="second pass: page body content")
    parser.add_argument("--opportunities", action="store_true",
                        help="the Opportunity Pipe database")
    args = parser.parse_args(argv)

    with connect() as conn:
        if args.bodies:
            counts = import_bodies(conn, apply=args.apply)
        elif args.opportunities:
            counts = import_opportunities(conn, apply=args.apply)
        else:
            counts = run(conn, apply=args.apply)
    if not args.apply:
        log.warning("DRY RUN, nothing written. Re-run with --apply.")
    log.info("%s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
