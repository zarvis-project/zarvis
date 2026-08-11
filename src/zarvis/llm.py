"""Model access. One small seam, deliberately provider-agnostic.

Ryan said at the outset he may move to Grok and does not want to be locked to
Claude. The cheapest way to honour that is to never import a vendor SDK: both
Anthropic and the OpenAI-compatible family (OpenAI, xAI/Grok, most local
servers) are plain JSON over HTTPS, and talking to them directly costs about
sixty lines and removes a whole class of dependency drift.

Switching provider is then two environment variables:

    ZARVIS_LLM_PROVIDER=openai
    ZARVIS_LLM_MODEL=grok-4
    ZARVIS_LLM_BASE_URL=https://api.x.ai/v1
    ZARVIS_LLM_API_KEY=...

Nothing above the seam knows which one answered.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import get_config

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 120

# Multiplier applied when a provider wants `max_completion_tokens`. That budget
# includes hidden reasoning tokens, which on these prompts run 5-10x the visible
# answer, so the caller's "I want ~1200 tokens of email" needs real headroom.
REASONING_HEADROOM = 6


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    # Billed as output, never shown. Broken out because the difference between
    # a model that reasons and one that does not is invisible in the reply and
    # decisive in both cost and quality.
    reasoning_tokens: int = 0
    # Cached input bills at a fraction of fresh input: $0.30 vs $2.00 per
    # million on grok-4.5. Tracked separately so the saving is visible.
    cached_tokens: int = 0
    # Some providers report what they actually billed. When present this beats
    # anything the local price table can compute.
    billed_ticks: int | None = None

    def cost_usd(self, prices: dict | None = None) -> float:
        """What this call cost.

        Prefers the provider's own figure. xAI returns `cost_in_usd_ticks` on
        every response, which is what was actually billed and is not subject to
        my price table drifting or to me misreading a units scale, both of which
        have already happened once each in this project.

        Falls back to the table for providers that report nothing.
        """
        if self.billed_ticks is not None:
            return self.billed_ticks / TICKS_PER_USD

        table = prices or PRICES
        model = self.model_id.lower()
        # Match at a version boundary only: plain startswith would price
        # "gpt-5.4" at the "gpt-5" rate, which is a confident wrong number for
        # exactly the models most likely to have new pricing.
        matches = sorted(
            (k for k in table if model == k or model.startswith(k + "-")),
            key=len, reverse=True,
        )
        if not matches:
            log.warning(
                "no price entry for %s; using default rate. Token counts are "
                "measured, this dollar figure is not.", self.model_id,
            )
        rate_in, rate_out, rate_cached = table[matches[0]] if matches else DEFAULT_PRICE

        if self.input_tokens >= LONG_CONTEXT_THRESHOLD:
            rate_in *= LONG_CONTEXT_MULTIPLIER
            rate_out *= LONG_CONTEXT_MULTIPLIER
            rate_cached *= LONG_CONTEXT_MULTIPLIER

        fresh = max(0, self.input_tokens - self.cached_tokens)
        return (
            fresh * rate_in
            + self.cached_tokens * rate_cached
            + self.output_tokens * rate_out
        ) / 1_000_000


# Per million tokens, (input, output, cached_input).
#
# Verified 2026-08-09 against official vendor pricing pages. See
# docs/model-pricing.md for sources, the long-context rules, and the reasoning
# accounting differences between providers.
#
# These are a FALLBACK. xAI reports what it actually billed on every response
# (`cost_in_usd_ticks`), and that is preferred wherever present. This table
# covers providers that report nothing.
PRICES: dict[str, tuple[float, float, float]] = {
    "grok-4.5":         (2.00,  6.00,  0.30),
    "grok-4.3":         (1.25,  2.50,  0.20),
    "grok-4.20":        (1.25,  2.50,  0.20),
    "grok-build-0.1":   (1.00,  2.00,  0.20),
    "gpt-5.4":          (2.50, 15.00,  0.25),
    "gpt-5.4-mini":     (0.75,  4.50,  0.075),
    "gpt-5":            (1.25, 10.00,  0.125),
    "gpt-5-mini":       (0.25,  2.00,  0.025),
    "claude-opus-5":    (5.00, 25.00,  0.50),
    # Intro pricing through 2026-08-31, then 3.00 / 15.00.
    "claude-sonnet-5":  (2.00, 10.00,  0.20),
    "claude-haiku-4-5": (1.00,  5.00,  0.10),
}
DEFAULT_PRICE = (5.00, 25.00, 0.50)

# xAI: 1 USD = 10,000,000,000 ticks.
TICKS_PER_USD = 10_000_000_000

# At or above this prompt size xAI charges the higher rate on EVERY token in the
# request, not just the excess. Roughly 2x.
LONG_CONTEXT_THRESHOLD = 200_000
LONG_CONTEXT_MULTIPLIER = 2.0


def _post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise LLMError(f"{exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"could not reach {url}: {exc.reason}") from exc


# Anthropic takes a token budget, the OpenAI family takes a named level. One
# mapping so callers can say "high" and mean it everywhere.
THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


def _anthropic(
    system: str, user: str, *, model: str, key: str, max_tokens: int,
    base_url: str | None = None, effort: str | None = None,
) -> Completion:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if effort and effort != "none":
        budget = THINKING_BUDGET.get(effort, 4096)
        # max_tokens must exceed the thinking budget, it is not additional to it.
        payload["max_tokens"] = max(max_tokens, budget + max_tokens)
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    data = _post(
        (base_url or get_config().llm_base_url or "https://api.anthropic.com") + "/v1/messages",
        payload,
        {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    thinking = sum(
        len(b.get("thinking", "")) // 4
        for b in data.get("content", [])
        if b.get("type") == "thinking"
    )
    usage = data.get("usage", {})
    return Completion(
        text="".join(parts),
        model_id=data.get("model", model),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        reasoning_tokens=thinking,
    )


def _openai_compatible(
    system: str, user: str, *, model: str, key: str, max_tokens: int,
    base_url: str | None = None, effort: str | None = None,
) -> Completion:
    base = base_url or get_config().llm_base_url or "https://api.openai.com/v1"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # gpt-5.4 defaults to NO reasoning: measured at exactly 0 reasoning tokens
    # unless asked. That is a reasonable default for chat and the wrong one for
    # deciding how to re-open a stalled six-figure relationship.
    if effort:
        payload["reasoning_effort"] = effort
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
    url = base.rstrip("/") + "/chat/completions"

    # "OpenAI-compatible" is a family, not a spec. OpenAI's newer models rejected
    # `max_tokens` outright and require `max_completion_tokens`; xAI still takes
    # `max_tokens`. Rather than hard-code which vendor wants which — a mapping
    # that goes stale the next time either one ships — try the older spelling and
    # fall back on the specific error that says so.
    try:
        data = _post(url, {**payload, "max_tokens": max_tokens}, headers)
    except LLMError as exc:
        if "max_completion_tokens" not in str(exc):
            raise
        log.info("%s rejects max_tokens, retrying with max_completion_tokens", model)
        # `max_completion_tokens` is a budget for reasoning AND output, where
        # `max_tokens` capped visible output alone. A model that rejects the old
        # spelling is a reasoning model, and on a 4,000 token prompt it spent the
        # entire 1,200 budget thinking and returned an empty string with
        # finish_reason "stop" — a silent empty answer, not an error.
        data = _post(
            url,
            {**payload, "max_completion_tokens": max_tokens * REASONING_HEADROOM},
            headers,
        )
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"no choices in response: {json.dumps(data)[:400]}")
    usage = data.get("usage", {})
    details = usage.get("completion_tokens_details") or {}
    text = choices[0].get("message", {}).get("content", "") or ""

    # "OpenAI-compatible" does not extend to how reasoning is counted, and the
    # difference is a silent 10x cost error.
    #
    #   OpenAI: total = prompt + completion, reasoning is INSIDE completion.
    #   xAI:    total = prompt + completion + reasoning, reasoning is EXTRA.
    #
    # Verified against xAI's own `cost_in_usd_ticks`: reconstructing the bill
    # only matches when reasoning is added to completion. Taking completion at
    # face value undercounted grok-4.5 by roughly 10x on these prompts, because
    # it reasons hard and reports almost none of it in `completion_tokens`.
    #
    # Detected from the arithmetic rather than from the vendor name, so a new
    # provider is handled without a mapping to forget to update.
    completion = usage.get("completion_tokens", 0)
    reasoning = details.get("reasoning_tokens", 0)
    prompt = usage.get("prompt_tokens", 0)
    total = usage.get("total_tokens", 0)
    billable_output = (
        completion + reasoning
        if total and total > prompt + completion
        else completion
    )
    if not text.strip():
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        raise LLMError(
            f"{model} returned empty content "
            f"(finish_reason={choices[0].get('finish_reason')}, "
            f"reasoning_tokens={reasoning}). The token budget was consumed before "
            f"it could answer; raise max_tokens."
        )
    return Completion(
        text=text,
        model_id=data.get("model", model),
        input_tokens=prompt,
        output_tokens=billable_output,
        reasoning_tokens=reasoning,
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        billed_ticks=usage.get("cost_in_usd_ticks"),
    )


def complete(
    system: str,
    user: str,
    *,
    max_tokens: int = 1500,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    effort: str | None = None,
) -> Completion:
    """Generate. Overrides exist so the same prompt can be run against several
    providers without touching config, which is what makes a bake-off cheap."""
    cfg = get_config()
    provider = (provider or cfg.llm_provider).lower()
    model = model or cfg.llm_model
    api_key = api_key or cfg.llm_api_key
    if not api_key:
        raise LLMError(
            "ZARVIS_LLM_API_KEY is not set. Compose cannot run without it."
        )
    if provider == "anthropic":
        fn = _anthropic
    elif provider in {"openai", "grok", "xai", "openai-compatible"}:
        fn = _openai_compatible
    else:
        raise LLMError(f"unknown ZARVIS_LLM_PROVIDER: {cfg.llm_provider}")

    completion = fn(
        system, user, model=model, key=api_key, max_tokens=max_tokens,
        base_url=base_url, effort=effort,
    )
    log.debug(
        "llm %s in=%d out=%d",
        completion.model_id,
        completion.input_tokens,
        completion.output_tokens,
    )
    # Ledger every call. Imported here rather than at module scope to keep this
    # file free of a database import at load time, and wrapped because
    # accounting must never be able to break a run.
    try:
        from .costs import record

        record(completion)
    except Exception:  # noqa: BLE001
        pass
    return completion
