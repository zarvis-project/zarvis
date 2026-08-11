"""Google auth via service account + domain-wide delegation.

Why this and not an OAuth client:

- An OAuth consent screen in **Testing** status issues refresh tokens that expire
  after **7 days**. On an always-on VM that is a guaranteed 3am failure.
- Domain-wide delegation issues no refresh token at all, needs no consent screen,
  and no interactive login. Nothing to babysit.
- Because the Workspace app is Internal, there is no OAuth verification and no
  CASA security assessment.

Setup (once, allow ~24h to propagate):
  1. GCP project -> create service account -> create JSON key
  2. Note the service account's numeric Client ID
  3. Google Workspace Admin -> Security -> Access and data control ->
     API controls -> Manage Domain-Wide Delegation -> Add new
  4. Client ID = the numeric ID; OAuth scopes = the two read-only scopes below

The scopes here are READ-ONLY on purpose. The deciding process cannot write to
the mailbox at all; a separate writer service holds `gmail.compose` and accepts
only already-approved draft rows. That split is the never-send guarantee — there
is no Gmail scope that grants drafting without also granting send.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from .config import get_config

log = logging.getLogger(__name__)

# The google-api-python-client imports live INSIDE the functions below, not at
# module scope. Ingest promises that one failing source cannot kill the run, and
# a module-level import breaks that promise before the try/except in the runner
# ever gets a chance: an uninstalled package or a missing key takes down product
# ingest too, which has nothing to do with Google.
#
# Import failures now surface as "gmail source unavailable" rather than a
# traceback at startup.


@lru_cache(maxsize=1)
def _credentials() -> Any:
    from google.oauth2 import service_account

    cfg = get_config()
    creds = service_account.Credentials.from_service_account_file(
        cfg.google_sa_key_path, scopes=cfg.google_scopes
    )
    # Act as the operator. The key can impersonate any user in the domain, so
    # it is the single most sensitive secret Zarvis holds — 0600, owned by the
    # zarvis user, ideally sealed via systemd LoadCredentialEncrypted.
    return creds.with_subject(cfg.google_impersonate)


@lru_cache(maxsize=1)
def gmail_service() -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def calendar_service() -> Any:
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def execute(request: Any, *, attempts: int = 6) -> Any:
    """Run a Google API request, backing off when the quota says wait.

    Gmail reports rate limiting as HTTP 403 with reason `rateLimitExceeded`,
    which is indistinguishable from a real permission failure by status code
    alone — so the reason is checked, and anything else is re-raised immediately
    rather than retried six times against a wall.

    Needed because the backfill issues list calls in a tight loop and the
    per-minute unit budget is consumed in seconds. Retrying is correct here: the
    quota is a speed limit, not a refusal.
    """
    import time

    from googleapiclient.errors import HttpError

    RETRYABLE_REASONS = {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "backendError",
        "internalError",
    }

    delay = 2.0
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as exc:
            reasons = {
                d.get("reason", "") for d in (exc.error_details or []) if isinstance(d, dict)
            }
            retryable = bool(reasons & RETRYABLE_REASONS) or exc.resp.status in (500, 503)
            if not retryable or attempt == attempts - 1:
                raise
            log.warning(
                "google api rate limited (%s), retrying in %.0fs [%d/%d]",
                ",".join(sorted(reasons)) or exc.resp.status,
                delay,
                attempt + 1,
                attempts,
            )
            time.sleep(delay)
            delay *= 2


# ---------------------------------------------------------------------------
# The writer credential
# ---------------------------------------------------------------------------
# Separate from `gmail_service()` on purpose, and separate is all it is.
#
# `gmail.compose` grants `messages.send` as well as draft creation. There is no
# Gmail scope that allows drafting and forbids sending, so this split does NOT
# make sending impossible; it makes the write capability reachable from exactly
# one module. What actually enforces the never-send rule is `deliver.py` calling
# a single endpoint and `tests/test_no_send.py` failing the build if any module
# in the codebase names a send endpoint.
#
# Keeping the functions apart still buys something real: every module that
# reasons, ranks or drafts holds read-only credentials and cannot reach this one
# by accident.
# `gmail.modify` rather than `gmail.compose`, for one reason: labelling a draft
# needs `messages.modify`, and compose cannot touch labels at all (403 on even
# listing them).
#
# It is a genuinely wider grant. Modify covers archiving, trashing and
# bulk-modifying every message in the mailbox, not just drafts. Google publishes
# nothing narrower, so the restriction cannot live in the scope and lives in
# `tests/test_no_send.py` instead: drafts may be created, updated, deleted and
# labelled; messages may never be trashed, deleted, bulk-modified or sent, and
# the build fails if any module names one of those endpoints.
#
# That is a code guarantee rather than a Google one, which Ryan accepted
# knowingly. It stops mistakes and anything that would show up in a diff. It
# would not stop someone determined to get around it.
WRITER_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _writer_credentials() -> Any:
    from google.oauth2 import service_account

    cfg = get_config()
    creds = service_account.Credentials.from_service_account_file(
        cfg.google_sa_key_path, scopes=WRITER_SCOPES
    )
    return creds.with_subject(cfg.google_impersonate)


@lru_cache(maxsize=1)
def gmail_writer_service() -> Any:
    from googleapiclient.discovery import build

    return build(
        "gmail", "v1", credentials=_writer_credentials(), cache_discovery=False
    )
