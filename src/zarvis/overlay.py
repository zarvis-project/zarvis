"""Apply the judgment overlay.

    python -m zarvis.overlay --dry-run
    python -m zarvis.overlay

Reads `seed/03-judgment.toml` — the values the product cannot know. The seed
gives all 73 people a placeholder impact derived from seat tier; this is where
Ryan's actual read on what is at stake gets written down.

Two things here have no equivalent anywhere else in the system:

  * `suppressed_until` — deliberate silence with a reason and an escape
    condition. Not "no action needed": an active decision to wait, shown back in
    the brief so it leaves his head.
  * `private_notes` — context that informs ranking and must NEVER reach the
    drafting model. Written as a signal with is_private = true, which the
    evidence bundler counts but excludes.

Idempotent. Safe to re-run after editing the file.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import tomllib
from datetime import UTC, datetime, time

import psycopg
from psycopg.types.json import Json

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.overlay")

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[2] / "seed" / "03-judgment.toml"

# Fields the overlay owns. Anything not listed is left alone.
PERSON_FIELDS = (
    "impact", "self_sustaining", "path_override", "preferred_channel",
    "suppressed_until", "suppress_reason", "suppress_override",
)


def _as_datetime(value) -> datetime | None:
    """Coerce whatever the file gave us into a timestamptz.

    TOML types a BARE `2026-05-12` as a real date object but a QUOTED
    `"2026-05-12"` as a plain string, and the file uses quotes. Accept both, so
    the format does not depend on anyone remembering which one TOML treats as
    special.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip())
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.combine(value, time.min, tzinfo=UTC)


def _write_note(
    cur, workspace_id: str, person_id: str, text: str, *, private: bool, tag: str
) -> int:
    """Standing context as a signal. Never expires — TTL_DAYS['note'] is None.

    `source_ref` is stable per person and tag, so re-running updates nothing
    rather than piling up duplicates.
    """
    cur.execute(
        """
        insert into zarvis.signal (
            workspace_id, person_id, source, source_ref, kind, value, body,
            authored_by, is_private, observed_at, expires_at
        ) values (
            %s, %s, 'overlay', %s, 'note', %s, %s, 'operator', %s, now(), null
        )
        on conflict do nothing
        """,
        (
            workspace_id, person_id, f"overlay:{person_id}:{tag}",
            Json({"tag": tag}), text.strip(), private,
        ),
    )
    return cur.rowcount


def _add_identity(cur, workspace_id: str, person_id: str, kind: str, value: str) -> int:
    cur.execute(
        """
        insert into zarvis.person_identity (workspace_id, person_id, kind, value)
        values (%s, %s, %s, %s)
        on conflict (workspace_id, kind, value) do nothing
        """,
        (workspace_id, person_id, kind, value),
    )
    return cur.rowcount


def _seed_touch(cur, workspace_id: str, person_id: str, when: datetime) -> int:
    """A known last-contact date, so decay pressure has something to measure.

    Without this a person seeded yesterday looks freshly contacted, and decay —
    half the urgency model — never fires for them.
    """
    cur.execute(
        """
        insert into zarvis.touch (
            workspace_id, person_id, channel, direction, at, external_ref
        )
        select %s, %s, 'email', 'outbound', %s, 'overlay:last_touch'
        where not exists (
            select 1 from zarvis.touch
            where person_id = %s and external_ref = 'overlay:last_touch'
        )
        """,
        (workspace_id, person_id, when, person_id),
    )
    return cur.rowcount


def apply_overlay(conn: psycopg.Connection, path: pathlib.Path, *, dry_run: bool) -> dict:
    cfg = get_config()
    ws = cfg.workspace_id
    doc = tomllib.loads(path.read_text(encoding="utf-8"))

    counts = {
        "prospects_created": 0, "prospects_matched": 0, "overlays_applied": 0,
        "overlays_missed": 0, "identities": 0, "notes": 0, "private_notes": 0,
        "touches": 0,
    }
    missed: list[str] = []

    with conn.cursor() as cur:
        # --- prospects: no product record, so create outright ---------------
        for entry in doc.get("prospect", []):
            email = (entry.get("email") or "").strip().lower()
            name = entry.get("full_name", "?")
            if not email:
                missed.append(f"prospect {name}: no email in the file")
                counts["overlays_missed"] += 1
                continue

            cur.execute(
                "select id from zarvis.person where workspace_id=%s and lower(primary_email)=%s",
                (ws, email),
            )
            row = cur.fetchone()

            org_id = None
            if entry.get("org"):
                cur.execute(
                    """
                    insert into zarvis.org (workspace_id, name, domain)
                    select %s, %s, %s
                    where not exists (
                        select 1 from zarvis.org where workspace_id=%s and lower(domain)=%s
                    )
                    returning id
                    """,
                    (ws, entry["org"], entry["org"], ws, entry["org"].lower()),
                )
                created = cur.fetchone()
                if created:
                    org_id = created["id"]
                else:
                    cur.execute(
                        "select id from zarvis.org where workspace_id=%s and lower(domain)=%s",
                        (ws, entry["org"].lower()),
                    )
                    found = cur.fetchone()
                    org_id = found["id"] if found else None

            if row:
                person_id = str(row["id"])
                counts["prospects_matched"] += 1
            else:
                cur.execute(
                    """
                    insert into zarvis.person (
                        workspace_id, org_id, full_name, primary_email,
                        preferred_channel, impact, self_sustaining
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (
                        ws, org_id, name, email,
                        entry.get("preferred_channel", "email"),
                        entry.get("impact", 5), entry.get("self_sustaining", 0),
                    ),
                )
                person_id = str(cur.fetchone()["id"])
                counts["prospects_created"] += 1

            counts["identities"] += _add_identity(cur, ws, person_id, "email", email)
            if entry.get("phone"):
                counts["identities"] += _add_identity(
                    cur, ws, person_id, "phone", str(entry["phone"])
                )

            # Not everyone created here is a prospect. Ryan's VA is a `va`; a
            # teammate is `team`. Roles are rows, so an entry can carry several.
            for role in entry.get("roles", ["prospect"]):
                cur.execute(
                    """
                    insert into zarvis.relationship (workspace_id, person_id, role, status)
                    values (%s, %s, %s, 'active')
                    on conflict (person_id, role) do nothing
                    """,
                    (ws, person_id, role),
                )

            if entry.get("last_touch"):
                counts["touches"] += _seed_touch(
                    cur, ws, person_id, _as_datetime(entry["last_touch"])
                )
            if entry.get("notes"):
                counts["notes"] += _write_note(
                    cur, ws, person_id, entry["notes"], private=False, tag="context"
                )

        # --- overlays: people already seeded from the product ---------------
        for entry in doc.get("overlay", []):
            email = (entry.get("match_email") or "").strip().lower()
            if not email:
                counts["overlays_missed"] += 1
                continue

            cur.execute(
                """
                select p.id from zarvis.person p
                where p.workspace_id = %s and (
                    lower(p.primary_email) = %s
                    or exists (
                        select 1 from zarvis.person_identity i
                        where i.person_id = p.id and i.kind = 'email'
                          and lower(i.value) = %s
                    )
                )
                """,
                (ws, email, email),
            )
            row = cur.fetchone()
            if not row:
                missed.append(f"overlay {email}: no matching person")
                counts["overlays_missed"] += 1
                continue

            person_id = str(row["id"])
            updates = {f: entry[f] for f in PERSON_FIELDS if f in entry}
            if "suppressed_until" in updates:
                updates["suppressed_until"] = _as_datetime(updates["suppressed_until"])

            if updates:
                sets = ", ".join(f"{k} = %({k})s" for k in updates)
                cur.execute(
                    f"update zarvis.person set {sets} where id = %(id)s",
                    {**updates, "id": person_id},
                )
                counts["overlays_applied"] += 1

            # A second address for the same human. Without this, calendar and
            # product signals for one person never join up.
            for alias in entry.get("also_known_as", []):
                counts["identities"] += _add_identity(
                    cur, ws, person_id, "email", alias.strip().lower()
                )

            if entry.get("last_touch"):
                counts["touches"] += _seed_touch(
                    cur, ws, person_id, _as_datetime(entry["last_touch"])
                )
            if entry.get("notes"):
                counts["notes"] += _write_note(
                    cur, ws, person_id, entry["notes"], private=False, tag="context"
                )
            if entry.get("private_notes"):
                counts["private_notes"] += _write_note(
                    cur, ws, person_id, entry["private_notes"], private=True, tag="private"
                )

    if dry_run:
        conn.rollback()
        log.info("DRY RUN — rolled back, nothing persisted")
    else:
        conn.commit()

    for m in missed:
        log.warning("%s", m)
    return counts


def _apply_links(conn: psycopg.Connection, entries: list[dict], *, dry_run: bool) -> int:
    """Person-to-person edges that only Ryan knows.

    The Notion import supplied 137 introduction edges, but the ones that decide
    WHO TO EMAIL are usually unwritten. One operator sits on 343 unapproved
    requests and the person who can do anything about it is Priya Raman, who holds
    the budget. Nothing in any product table says so: they share a domain and
    nothing else, and inferring authority from a shared domain would promote
    every colleague to decision maker.

    So these are declared, not derived.
    """
    ws = get_config().workspace_id
    applied = 0
    for entry in entries:
        with conn.cursor() as cur:
            cur.execute(
                """
                select p.id, p.full_name from zarvis.person p
                join zarvis.person_identity i on i.person_id = p.id
                where p.workspace_id = %s and lower(i.value) = lower(%s)
                limit 1
                """,
                (ws, entry["from_email"]),
            )
            src = cur.fetchone()
            cur.execute(
                """
                select p.id, p.full_name from zarvis.person p
                join zarvis.person_identity i on i.person_id = p.id
                where p.workspace_id = %s and lower(i.value) = lower(%s)
                limit 1
                """,
                (ws, entry["to_email"]),
            )
            dst = cur.fetchone()
        if not src or not dst:
            log.warning(
                "link skipped, unknown person: %s -> %s",
                entry["from_email"], entry["to_email"],
            )
            continue

        log.info(
            "link: %s -%s-> %s", src["full_name"], entry["kind"], dst["full_name"]
        )
        applied += 1
        if dry_run:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into zarvis.link
                    (workspace_id, from_person, to_person, kind, value_accrues_to,
                     strength, note, status)
                values (%s, %s, %s, %s,
                        (select p.id from zarvis.person p
                          join zarvis.person_identity i on i.person_id = p.id
                          where lower(i.value) = lower(%s) limit 1),
                        %s, %s, 'active')
                on conflict (workspace_id, from_person, to_person, kind)
                do update set note = excluded.note,
                              strength = excluded.strength,
                              value_accrues_to = excluded.value_accrues_to,
                              updated_at = now()
                """,
                (ws, src["id"], dst["id"], entry["kind"],
                 entry.get("value_accrues_to") or entry["from_email"],
                 entry.get("strength", 0.8), entry.get("note")),
            )
        conn.commit()
    return applied


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stderr
    )
    parser = argparse.ArgumentParser(description="Apply the judgment overlay")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--file", type=pathlib.Path, default=DEFAULT_PATH)
    args = parser.parse_args()

    log.info("reading %s", args.file)
    with connect() as conn:
        counts = apply_overlay(conn, args.file, dry_run=args.dry_run)
        import tomllib
        data = tomllib.loads(args.file.read_text(encoding="utf-8"))
        counts["links"] = _apply_links(
            conn, data.get("link", []), dry_run=args.dry_run
        )
    for key, value in counts.items():
        log.info("%-20s %s", key, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
