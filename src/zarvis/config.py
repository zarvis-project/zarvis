"""Configuration. Everything comes from the environment; nothing is hardcoded.

Secrets live in systemd LoadCredentialEncrypted (preferred) or a 0600 .env owned
by the `zarvis` user. See .env.example.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from dotenv import find_dotenv, load_dotenv

# Precedence: shell environment > .env.local > .env
#
# Both loads use override=False, so the FIRST source to define a key wins and
# anything already in os.environ (i.e. set by the shell) beats both files. That
# is what makes a one-off `ZARVIS_DRY_RUN=0 python -m zarvis.ingest` work.
#
# An earlier version loaded .env.local with override=True, which meant the file
# silently beat the command line — you could set a variable in the shell, watch
# it have no effect, and have nothing to tell you why.
#
# python-dotenv only reads `.env` by default, so `.env.local` has to be named
# explicitly or it is ignored entirely. `.env.example` is the committed
# template; `.env` and `.env.local` are gitignored and hold real values.
load_dotenv(find_dotenv(".env.local", usecwd=True), override=False)
load_dotenv(find_dotenv(".env", usecwd=True), override=False)


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing.

    Fail loudly here rather than halfway through a run against the production
    database.
    """


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set. See .env.example.")
    return value


# A domain is a domain. Validated on the way in so that
# `synthetic_email_exclusion` below can inline these into SQL without opening an
# injection hole: the check is what makes the inlining safe, and removing one
# without the other is the bug to watch for.
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def _domains(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    out = []
    for part in raw.split(","):
        part = part.strip().lower().lstrip("@")
        if not part:
            continue
        if not _DOMAIN_RE.match(part):
            raise ConfigError(
                f"{name}: {part!r} is not a valid domain. Comma separated, "
                f"like 'placeholder.example.com,managed.example.internal'."
            )
        out.append(part)
    return tuple(out)


def synthetic_email_exclusion(column: str, *, escaped: bool = True) -> str:
    """SQL that excludes manufactured addresses, or nothing if none configured.

    Returned as a fragment rather than a parameter because these queries embed
    it inside correlated subqueries in a SELECT list, where getting a positional
    parameter into the right slot means renumbering every other one at five call
    sites. The domains are operator configuration validated against
    `_DOMAIN_RE`, not user input.

    Empty configuration yields an empty string, so the default deployment runs
    the same query it always did with no filter at all.
    """
    domains = get_config().synthetic_email_domains
    if not domains:
        return ""
    # `%%` is how a literal percent survives psycopg's parameter interpolation,
    # but that pass only happens when params are actually passed. A query with
    # params=None is sent verbatim, so the same string would ask Postgres for
    # addresses beginning with a literal "%". Hence the flag, set by the caller
    # that knows.
    pct = "%%" if escaped else "%"
    patterns = ", ".join(f"'{pct}@{d}'" for d in domains)
    return f"and {column} not like all (array[{patterns}])"


_SENTINEL_RE = re.compile(r"/\*SYNTH:([a-z_.]+)\*/")


def expand_sql(sql: str, *, escaped: bool = True) -> str:
    """Replace `/*SYNTH:col*/` markers with the exclusion clause.

    The marker is a SQL COMMENT, which is the point: if this expansion is ever
    skipped the query still parses and still runs. It degrades to "do not filter
    synthetic addresses" rather than to a syntax error at 08:00 on a Sunday.
    """
    return _SENTINEL_RE.sub(
        lambda m: synthetic_email_exclusion(m.group(1), escaped=escaped), sql
    )


@dataclass(frozen=True)
class Config:
    # --- database -----------------------------------------------------------
    database_url: str
    workspace_id: str

    # --- google -------------------------------------------------------------
    # Service account + domain-wide delegation, impersonating the operator.
    # NOT an OAuth client in Testing status: that issues refresh tokens which
    # expire after 7 days, which is a trap on an always-on VM.
    google_sa_key_path: str
    google_impersonate: str

    # --- synthetic addresses ------------------------------------------------
    # Some products manufacture email addresses for accounts that never gave a
    # real one, so that every row has something in the column. They cannot match
    # a message and they cannot receive one, and Gmail bills the `q` parameter by
    # the query, so searching for them costs real quota to learn nothing.
    #
    # Empty by default. This is deployment-specific: it describes a quirk of
    # whatever system you ingest from, not of Zarvis. Set it to the domains your
    # product invents, comma separated.
    synthetic_email_domains: tuple[str, ...]

    # --- queue tiers --------------------------------------------------------
    # The queue is a standing agenda of ~40 live decisions, re-scored each run
    # rather than regenerated. Tiers are derived from rank:
    #   1..priority_size                    -> priority (drafted, shown in full)
    #   ..+standard_size                    -> standard (one-liners, promotable)
    #   ..+backlog_size                     -> backlog  (collapsed to a count)
    #   beyond                              -> dormant  (kept and scored, hidden)
    #
    # Drafting is capped at the priority tier. Scoring forty items is SQL and
    # free; composing forty emails is not, and Ryan will only action five.
    tier_priority: int
    tier_standard: int
    tier_backlog: int

    # --- safety -------------------------------------------------------------
    dry_run: bool
    kill_switch: bool
    min_days_between_touches: int
    cost_ceiling_usd: float

    @property
    def queue_size(self) -> int:
        """Everything past this is scored but goes dormant."""
        return self.tier_priority + self.tier_standard + self.tier_backlog

    def tier_for_rank(self, rank: int) -> str:
        """Rank is 1-based. Tier is always derived, never stored as intent."""
        if rank <= self.tier_priority:
            return "priority"
        if rank <= self.tier_priority + self.tier_standard:
            return "standard"
        if rank <= self.queue_size:
            return "backlog"
        return "dormant"

    # --- composition --------------------------------------------------------
    # Provider is a setting, not an assumption. Ryan may move to Grok, and the
    # cost of that should be one env var rather than a rewrite — so llm.py talks
    # raw HTTP to whichever endpoint this names and no vendor SDK is imported
    # anywhere in the codebase.
    llm_provider: str
    llm_model: str
    llm_api_key: str | None
    llm_base_url: str | None

    # --- observability ------------------------------------------------------
    heartbeat_url: str | None

    @property
    def google_scopes(self) -> list[str]:
        """Read-only. The deciding process cannot write to the mailbox.

        `gmail.modify` would grant draft creation AND `messages.send` — there is
        no Gmail scope that allows drafting without allowing sending. The
        never-send guarantee is therefore a process split, not a scope choice:
        a separate writer service holds `gmail.compose` and accepts only
        already-approved draft rows.
        """
        return [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ]


@lru_cache(maxsize=1)
def get_config() -> Config:
    _provider = os.environ.get("ZARVIS_LLM_PROVIDER", "xai")
    return Config(
        database_url=_require("ZARVIS_DATABASE_URL"),
        workspace_id=_require("ZARVIS_WORKSPACE_ID"),
        google_sa_key_path=_require("ZARVIS_GOOGLE_SA_KEY_PATH"),
        google_impersonate=_require("ZARVIS_GOOGLE_IMPERSONATE"),
        synthetic_email_domains=_domains("ZARVIS_SYNTHETIC_EMAIL_DOMAINS"),
        tier_priority=int(os.environ.get("ZARVIS_TIER_PRIORITY", "5")),
        tier_standard=int(os.environ.get("ZARVIS_TIER_STANDARD", "10")),
        tier_backlog=int(os.environ.get("ZARVIS_TIER_BACKLOG", "25")),
        dry_run=os.environ.get("ZARVIS_DRY_RUN", "0") == "1",
        kill_switch=os.environ.get("ZARVIS_KILL_SWITCH", "0") == "1",
        min_days_between_touches=int(os.environ.get("ZARVIS_MIN_DAYS_BETWEEN_TOUCHES", "7")),
        cost_ceiling_usd=float(os.environ.get("ZARVIS_COST_CEILING_USD", "2.00")),
        llm_provider=_provider,
        llm_model=os.environ.get("ZARVIS_LLM_MODEL", "grok-4.5"),
        # Fall back to the provider-specific key so switching provider is one
        # variable, not two, and the bake-off keys already on disk just work.
        llm_api_key=(
            os.environ.get("ZARVIS_LLM_API_KEY")
            or os.environ.get(f"ZARVIS_LLM_API_KEY_{_provider.upper()}")
            or None
        ),
        llm_base_url=os.environ.get("ZARVIS_LLM_BASE_URL") or None,
        heartbeat_url=os.environ.get("ZARVIS_HEARTBEAT_URL") or None,
    )
