"""Run the same real draft through several models and read them side by side.

    PYTHONPATH=src python -m zarvis.bakeoff \
        --model anthropic:claude-opus-4-5 \
        --model openai:gpt-5 \
        --model xai:grok-4

Writes nothing. Touches no queue item, creates no draft row, sends nothing.

WHY THIS EXISTS
---------------
Ryan's prior is that OpenAI writes more human copy, formed over a year of Zenith
outreach. That is real evidence and worth more than any benchmark. It is also
evidence about a DIFFERENT JOB: those messages are cold outreach to strangers,
generated from strategy templates, where `humanize.md` applies in full and
unearned warmth reads as slop. Zarvis drafts are warm replies into live threads
with people he knows, where `voice.md` explicitly suspends the dial-it-down rule.

A model that is better at sounding human to a stranger is not automatically
better at sounding like Ryan to Priya. Rather than argue the point, generate both
on his actual people and let him read them.

Cost is not a tiebreaker at this volume. Four drafts a day is roughly 315k input
tokens a month, so even the most expensive option lands around the price of a
coffee. Choose on the writing.

API keys are read per provider so several can be compared in one run:
    ZARVIS_LLM_API_KEY_ANTHROPIC / ANTHROPIC_API_KEY
    ZARVIS_LLM_API_KEY_OPENAI    / OPENAI_API_KEY
    ZARVIS_LLM_API_KEY_XAI       / XAI_API_KEY
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .compose import _build_prompt, _draftable, _parse
from .config import get_config
from .db import connect
from .lint import blocking, check, sweep
from .llm import LLMError, complete

log = logging.getLogger("zarvis.bakeoff")

DEFAULT_BASE_URLS = {
    "xai": "https://api.x.ai/v1",
    "grok": "https://api.x.ai/v1",
    "openai": "https://api.openai.com/v1",
}


def _key_for(provider: str) -> str | None:
    for name in (
        f"ZARVIS_LLM_API_KEY_{provider.upper()}",
        f"{provider.upper()}_API_KEY",
    ):
        if os.environ.get(name):
            return os.environ[name]
    # Fall back to the configured key when comparing models from one vendor.
    return get_config().llm_api_key


def _spec(raw: str) -> tuple[str, str, str | None]:
    """`provider:model[:base_url]` -> parts. Model names contain no colons;
    base URLs do, so split from the left exactly twice."""
    bits = raw.split(":", 2)
    if len(bits) < 2:
        raise argparse.ArgumentTypeError(
            f"expected provider:model[:base_url], got {raw!r}"
        )
    provider, model = bits[0], bits[1]
    base = bits[2] if len(bits) > 2 else DEFAULT_BASE_URLS.get(provider.lower())
    return provider, model, base


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", action="append", required=True, type=_spec,
        help="provider:model[:base_url], repeatable",
    )
    parser.add_argument("--limit", type=int, default=2, help="how many people")
    parser.add_argument("--person", help="substring match on a name")
    parser.add_argument(
        "--effort", choices=("none", "low", "medium", "high"),
        help="reasoning effort. gpt-5.4 defaults to none, so this is not cosmetic.",
    )
    args = parser.parse_args(argv)

    with connect() as conn:
        items = _draftable(conn, get_config().queue_size)

    if args.person:
        needle = args.person.lower()
        items = [i for i in items if needle in (i["full_name"] or "").lower()]
    items = items[: args.limit]

    if not items:
        log.error("no draftable items matched")
        return 1

    for item in items:
        system, user, evidence_text = _build_prompt(item)
        first = (item["full_name"] or "").split()[0] or None

        print("\n" + "=" * 78)
        print(f"{item['full_name']}   ({item['play_key']})")
        print("=" * 78)

        for provider, model, base in args.model:
            key = _key_for(provider)
            if not key:
                print(f"\n--- {provider}:{model} --- SKIPPED, no API key\n")
                continue
            try:
                completion = complete(
                    system, user, max_tokens=1200,
                    provider=provider, model=model, api_key=key, base_url=base,
                    effort=args.effort,
                )
                _, body = _parse(completion.text)
            except LLMError as exc:
                print(f"\n--- {provider}:{model} --- FAILED: {exc}\n")
                continue

            body = sweep(body)
            findings = check(body, evidence_text=evidence_text, recipient_first_name=first)
            hard = blocking(findings)

            # Reasoning is billed as output and never shown, so it has to be
            # broken out or a model that thinks looks identical to one that does
            # not, right up until the invoice.
            visible = completion.output_tokens - completion.reasoning_tokens
            print(f"\n--- {provider}:{model}"
                  f"{' effort=' + args.effort if args.effort else ''} "
                  f"({completion.input_tokens} in / {visible} visible + "
                  f"{completion.reasoning_tokens} reasoning, "
                  f"~${completion.cost_usd():.4f}) ---\n")
            print(body)
            if findings:
                print("\n  linter:")
                for f in findings:
                    print(f"    [{'BLOCK' if f.blocking else 'note '}] {f.rule}: {f.detail}")
            if not hard:
                print("\n  linter: clean")

    print("\n" + "=" * 78)
    print("Nothing was written. No drafts created, no queue items touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
