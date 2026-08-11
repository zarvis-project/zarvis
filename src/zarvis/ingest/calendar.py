"""Calendar ingest: meetings that happened, and meetings that are coming.

Uses `events.list`, not `freebusy` — freebusy returns busy intervals only, with
no titles and no attendees, which is useless for matching a meeting to a person.

Push notifications are deliberately skipped: channels expire with no
auto-renewal, so you must re-`watch` before expiry forever. Daily polling is
strictly simpler and the latency is irrelevant to a daily agent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg

from ..config import get_config
from ..google_auth import calendar_service
from ..signals import Signal, resolve_person_by_email

log = logging.getLogger(__name__)

SOURCE = "calendar"
LOOKAHEAD_DAYS = 7

# Rooms, resources and the operator's own address are not counterparties.
_RESOURCE_MARKERS = ("resource.calendar.google.com", "@resource.")


def _is_real_attendee(email: str, operator: str) -> bool:
    if not email:
        return False
    email = email.lower()
    if email == operator.lower():
        return False
    return not any(marker in email for marker in _RESOURCE_MARKERS)


def _parse_dt(node: dict) -> datetime | None:
    """Google returns either dateTime (timed) or date (all-day)."""
    raw = node.get("dateTime") or node.get("date")
    if not raw:
        return None
    if len(raw) == 10:  # all-day: YYYY-MM-DD
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _authorship(event: dict, operator: str) -> str:
    """Who wrote the free-text in this event's description?

    This is the rule that came out of getting it wrong on real data. A Calendly
    booking carries a "please share anything that will help prepare" field, and
    it is filled in by **whoever booked**. If the operator is the invitee, that
    prose is his own words — a reminder to himself, never intelligence about the
    other party. Surfacing it as "here's something you should know" quotes him
    back to himself, which is small, corrosive, and destroys trust in the brief.
    """
    organizer = (event.get("organizer") or {}).get("email", "").lower()
    creator = (event.get("creator") or {}).get("email", "").lower()
    if operator.lower() in (organizer, creator):
        # Operator owns the event, so a booking form was filled by the other side.
        return "counterparty"
    # Someone else's calendar — the operator booked in, so the notes are his.
    return "operator"


def _attendee_person_ids(
    conn: psycopg.Connection, event: dict, operator: str
) -> list[str]:
    ids: list[str] = []
    for attendee in event.get("attendees") or []:
        email = (attendee.get("email") or "").lower()
        if not _is_real_attendee(email, operator):
            continue
        # A declined attendee did not attend. Treating a decline as a held
        # meeting would fire a follow-up for a conversation that never happened.
        if attendee.get("responseStatus") == "declined":
            continue
        person_id = resolve_person_by_email(conn, email)
        if person_id:
            ids.append(person_id)
    return ids


def ingest(conn: psycopg.Connection) -> list[Signal]:
    cfg = get_config()
    svc = calendar_service()
    operator = cfg.google_impersonate

    now = datetime.now(UTC)
    window_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = now + timedelta(days=LOOKAHEAD_DAYS)

    events: list[dict] = []
    page = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId="primary",
                timeMin=window_start.isoformat(),
                timeMax=window_end.isoformat(),
                singleEvents=True,      # expand recurrences; we want instances
                orderBy="startTime",
                maxResults=250,
                pageToken=page,
            )
            .execute()
        )
        events.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    signals: list[Signal] = []

    for event in events:
        if event.get("status") == "cancelled":
            continue
        # Blocks the operator put on his own calendar (focus time, reminders)
        # are marked transparent. They are not meetings.
        if event.get("transparency") == "transparent":
            continue

        start = _parse_dt(event.get("start") or {})
        if start is None:
            continue

        person_ids = _attendee_person_ids(conn, event, operator)
        if not person_ids:
            continue

        happened = start < now
        kind = "meeting_held" if happened else "meeting_scheduled"
        authored_by = _authorship(event, operator)
        description = event.get("description")

        for person_id in person_ids:
            signals.append(
                Signal(
                    source=SOURCE,
                    kind=kind,
                    observed_at=start,
                    person_id=person_id,
                    # iCalUID is stable across edits, so a follow-up fires once
                    # even if the meeting gets moved.
                    source_ref=event.get("iCalUID") or event.get("id"),
                    value={
                        "event_id": event.get("id"),
                        "summary": event.get("summary"),
                        "start": start.isoformat(),
                        "location": event.get("location"),
                        "attendee_count": len(person_ids),
                    },
                    body=description,
                    authored_by=authored_by,
                )
            )

    held = sum(1 for s in signals if s.kind == "meeting_held")
    log.info(
        "calendar: %d events in window, %d signals (%d held, %d upcoming)",
        len(events),
        len(signals),
        held,
        len(signals) - held,
    )
    return signals
