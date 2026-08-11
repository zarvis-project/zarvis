"""Seed people from product data.

    python -m zarvis.seed --dry-run     # report what would change
    python -m zarvis.seed               # apply

`v_user_state` turns out to carry almost everything needed to populate the
address book: names, emails, LinkedIn URLs, WhatsApp numbers, timezones, role
flags, and the agency->client edge. So the 73 existing users become people,
identities, roles and management edges automatically, and only prospects (who
have no product record at all) need entering by hand.

**Idempotent, and deliberately non-destructive.** Re-running updates only the
fields the product owns. Anything a human has tuned — `impact`,
`self_sustaining`, `path_override`, `suppressed_until`, `style_notes` — is set
on INSERT and never touched again, because the whole point of those columns is
that they encode judgment the product does not have.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from .config import get_config
from .db import connect

log = logging.getLogger("zarvis.seed")

# Email domains that say nothing about who someone works for.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "msn.com", "comcast.net", "verizon.net", "att.net",
}

# Product role flags -> zarvis.relationship roles.
ROLE_FLAGS = {
    "has_agency_function": "agency_owner",
    "has_client_function": "user",
    "has_approver_function": "approver",
}


@dataclass
class Counts:
    orgs: int = 0
    people: int = 0
    identities: int = 0
    relationships: int = 0
    manages: int = 0
    skipped_no_email: int = 0
    notes: list[str] = field(default_factory=list)

    def report(self) -> None:
        log.info("orgs           %d", self.orgs)
        log.info("people         %d", self.people)
        log.info("identities     %d", self.identities)
        log.info("relationships  %d", self.relationships)
        log.info("manages edges  %d", self.manages)
        if self.skipped_no_email:
            log.warning("skipped (no email) %d", self.skipped_no_email)
        for note in self.notes:
            log.warning("%s", note)


def _domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return None if domain in FREE_EMAIL_DOMAINS else domain


def _default_impact(row: dict) -> float:
    """A first guess, meant to be overridden by hand.

    Seat tier is the only proxy the product offers for how much is at stake.
    Everything real about impact — portfolio size, referral surface, whether
    losing them is recoverable — lives in Ryan's head and gets set later.
    """
    seats = row.get("seat_special_min_qty") or 0
    if seats >= 10:
        return 9.0
    if seats >= 5:
        return 8.0
    if seats >= 2:
        return 7.0
    if row.get("has_agency_function"):
        return 7.0
    if row.get("subscription_status") in ("active", "trialing"):
        return 6.0
    return 5.0


def _upsert_org(cur, workspace_id: str, domain: str, counts: Counts) -> str | None:
    cur.execute(
        "select id from zarvis.org where workspace_id = %s and lower(domain) = %s",
        (workspace_id, domain),
    )
    row = cur.fetchone()
    if row:
        return str(row["id"])
    cur.execute(
        """
        insert into zarvis.org (workspace_id, name, domain)
        values (%s, %s, %s)
        returning id
        """,
        (workspace_id, domain, domain),
    )
    counts.orgs += 1
    return str(cur.fetchone()["id"])


def _find_person(cur, workspace_id: str, zenith_user_id: str, email: str | None) -> str | None:
    """Match on the product id first, then email. Never create a duplicate."""
    cur.execute(
        """
        select person_id from zarvis.person_identity
        where workspace_id = %s and kind = 'zenith_user_id' and value = %s
        """,
        (workspace_id, zenith_user_id),
    )
    row = cur.fetchone()
    if row:
        return str(row["person_id"])

    if email:
        cur.execute(
            "select id from zarvis.person where workspace_id = %s and lower(primary_email) = %s",
            (workspace_id, email),
        )
        row = cur.fetchone()
        if row:
            return str(row["id"])
    return None


def _add_identity(cur, workspace_id: str, person_id: str, kind: str, value: str, counts: Counts):
    if not value:
        return
    cur.execute(
        """
        insert into zarvis.person_identity (workspace_id, person_id, kind, value)
        values (%s, %s, %s, %s)
        on conflict (workspace_id, kind, value) do nothing
        """,
        (workspace_id, person_id, kind, str(value)),
    )
    counts.identities += cur.rowcount


def seed_from_product(conn: psycopg.Connection, *, dry_run: bool) -> Counts:
    cfg = get_config()
    workspace_id = cfg.workspace_id
    counts = Counts()

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from zarvis.v_user_state")
        rows = cur.fetchall()
        log.info("read %d rows from v_user_state", len(rows))

        for row in rows:
            zenith_user_id = str(row.get("zenith_user_id") or "").strip()
            email = (row.get("email") or "").strip().lower() or None

            if not zenith_user_id:
                continue
            if not email:
                # Without an email there is no way to reach them and no reliable
                # way to dedupe them. Counted, not silently dropped.
                counts.skipped_no_email += 1
                continue

            org_id = None
            domain = _domain(email)
            if domain:
                org_id = _upsert_org(cur, workspace_id, domain, counts)

            person_id = _find_person(cur, workspace_id, zenith_user_id, email)

            # Users skew WhatsApp, prospects skew email. If they gave a WhatsApp
            # number, that is where they actually want to be reached.
            preferred = "whatsapp" if row.get("whatsapp") else "email"
            full_name = (row.get("full_name") or "").strip() or email

            if person_id is None:
                cur.execute(
                    """
                    insert into zarvis.person (
                        workspace_id, org_id, full_name, primary_email, timezone,
                        preferred_channel, impact, self_sustaining
                    ) values (%s, %s, %s, %s, %s, %s, %s, 0)
                    returning id
                    """,
                    (
                        workspace_id, org_id, full_name, email,
                        row.get("timezone"), preferred, _default_impact(row),
                    ),
                )
                person_id = str(cur.fetchone()["id"])
                counts.people += 1
            else:
                # Product-owned fields only. impact / self_sustaining /
                # path_override / suppressed_until / style_notes are human
                # judgment and must survive every re-run.
                cur.execute(
                    """
                    update zarvis.person set
                        org_id            = coalesce(%s, org_id),
                        full_name         = coalesce(nullif(%s, ''), full_name),
                        timezone          = coalesce(%s, timezone),
                        preferred_channel = coalesce(preferred_channel, %s)
                    where id = %s
                    """,
                    (org_id, full_name, row.get("timezone"), preferred, person_id),
                )

            _add_identity(cur, workspace_id, person_id, "email", email, counts)
            _add_identity(cur, workspace_id, person_id, "zenith_user_id", zenith_user_id, counts)
            if row.get("zenith_acct_id"):
                _add_identity(
                    cur, workspace_id, person_id, "zenith_acct_id",
                    str(row["zenith_acct_id"]), counts,
                )
            if row.get("linkedin_url"):
                _add_identity(
                    cur, workspace_id, person_id, "linkedin_urn",
                    str(row["linkedin_url"]), counts,
                )
            if row.get("whatsapp"):
                _add_identity(
                    cur, workspace_id, person_id, "phone", str(row["whatsapp"]), counts
                )

            # Roles as rows, from the product's own flags.
            roles = {role for flag, role in ROLE_FLAGS.items() if row.get(flag)}

            # A free-trial user is STILL A PROSPECT — they have not been closed.
            # They sit in the middle of the venn diagram and hold both roles at
            # once, which is precisely why roles are rows rather than a column.
            # It also happens to be where the highest-value plays live: trial
            # expiring AND zero approvals is one situation, not two.
            if (row.get("subscription_status") or "").lower() in (
                "", "trialing", "trial", "none", "past_due", "unpaid", "canceled"
            ):
                roles.add("prospect")

            for role in roles:
                cur.execute(
                    """
                    insert into zarvis.relationship (workspace_id, person_id, role, status)
                    values (%s, %s, %s, 'active')
                    on conflict (person_id, role) do nothing
                    """,
                    (workspace_id, person_id, role),
                )
                counts.relationships += cur.rowcount

            if row.get("lifecycle_former_customer"):
                cur.execute(
                    """
                    update zarvis.relationship set status = 'former'
                    where person_id = %s and role = 'user'
                    """,
                    (person_id,),
                )

        # --- agency -> client edges, second pass so both ends exist ---------
        cur.execute(
            """
            select v.zenith_user_id as client_id, v.created_by_agency_id as agency_id
            from zarvis.v_user_state v
            where v.created_by_agency_id is not null
            """
        )
        for edge in cur.fetchall():
            cur.execute(
                """
                insert into zarvis.manages (workspace_id, manager_id, managed_id, scope)
                select %(ws)s, mgr.person_id, mgd.person_id, 'reseller'
                from zarvis.person_identity mgr, zarvis.person_identity mgd
                where mgr.workspace_id = %(ws)s and mgr.kind = 'zenith_user_id'
                  and mgr.value = %(agency)s
                  and mgd.workspace_id = %(ws)s and mgd.kind = 'zenith_user_id'
                  and mgd.value = %(client)s
                  and mgr.person_id <> mgd.person_id
                on conflict (manager_id, managed_id, scope) do nothing
                """,
                {
                    "ws": workspace_id,
                    "agency": str(edge["agency_id"]),
                    "client": str(edge["client_id"]),
                },
            )
            counts.manages += cur.rowcount

    if dry_run:
        conn.rollback()
        log.info("DRY RUN — rolled back, nothing persisted")
    else:
        conn.commit()

    counts.notes.append(
        "Prospects are NOT seeded here — they have no product record. "
        "Four accounts on the board all need "
        "entering by hand, along with impact / self_sustaining / suppression on "
        "everyone in zarvis/seed/01-people.md."
    )
    return counts


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stderr
    )
    parser = argparse.ArgumentParser(description="Seed zarvis.person from product data")
    parser.add_argument("--dry-run", action="store_true", help="report, then roll back")
    args = parser.parse_args()

    with connect() as conn:
        counts = seed_from_product(conn, dry_run=args.dry_run)
    counts.report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
