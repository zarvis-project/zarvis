# Model pricing reference

**Captured:** 2026-08-09, from official vendor docs via Grok deep research.
**Status:** authoritative replacement for the guesses that were in `llm.py` before.

Prices change. Re-check before any real budgeting. What is recorded here is what
the official pages said on the date above, and the two structural facts that
matter more than the numbers are in "Reasoning tokens" and "The xAI integer
scale" below.

---

## The xAI integer scale — resolved

`/v1/language-models` returns integer price fields. They are **USD cents per 100
million tokens**.

| Field on grok-4.5 | Raw | Means |
|---|---|---|
| `prompt_text_token_price` | 20000 | $2.00 / M input |
| `cached_prompt_text_token_price` | 3000 | $0.30 / M cached input |
| `completion_text_token_price` | 60000 | $6.00 / M output |

**`cost_in_usd_ticks`: 1 USD = 10,000,000,000 ticks.** So
`cost_usd = ticks / 1e10`.

This is the important one. xAI reports what it actually billed on every
response, which beats any local price table. `llm.py` now prefers it and falls
back to the table only for providers that report nothing.

Earlier in this project I could not resolve whether the scale was $2.00 or
$0.20 per million and quoted a 10x range. It is **$2.00**. Every cost figure I
gave as "expensive reading" was the correct one.

---

## Rates, per million tokens

Long-context rule for xAI: at a prompt of **≥200k tokens the higher rate applies
to every token in that request**, not just the excess.

| Model | Input | Output | Cached in | Long-context (≥200k) |
|---|---|---|---|---|
| **grok-4.5** | $2.00 | $6.00 | $0.30 | $4.00 / $12.00 / $0.60 |
| grok-4.3 | $1.25 | $2.50 | $0.20 | $2.50 / $5.00 / $0.40 |
| grok-4.20-* (all variants) | $1.25 | $2.50 | $0.20 | $2.50 / $5.00 / $0.40 |
| grok-build-0.1 | $1.00 | $2.00 | $0.20 | $2.00 / $4.00 / $0.40 |
| gpt-5.4 | $2.50 | $15.00 | $0.25 | $5.00 / $22.50 (≥~272k) |
| gpt-5.4-mini | $0.75 | $4.50 | $0.075 | |
| gpt-5 | $1.25 | $10.00 | $0.125 | |
| claude-opus-5 | $5.00 | $25.00 | $0.50 | |
| claude-sonnet-5 | $2.00 → $3.00 from 2026-09-01 | $10.00 → $15.00 | $0.20 | intro pricing ends 2026-08-31 |

---

## Reasoning tokens — the part that causes surprise bills

Billed at the full **output** rate everywhere. The difference is in how they are
*reported*, and getting it wrong is a silent 10x error:

| Provider | Reported where | Included in the main count? |
|---|---|---|
| **xAI** | `completion_tokens_details.reasoning_tokens` | **No. Additive.** `total = prompt + completion + reasoning` |
| **OpenAI** | `output_tokens_details.reasoning_tokens` | **Yes.** Already inside `completion_tokens` |
| **Anthropic** | `output_tokens_details.thinking_tokens` | Yes, billed as output |

`llm.py` detects this from the arithmetic (`total > prompt + completion`) rather
than from the vendor name, so a new provider is handled without a mapping to
forget.

**grok-4.5 reasoning cannot be disabled.** `reasoning_effort` accepts
`low | medium | high`, default **high**. That is why every grok-4.5 call in this
project carries several hundred invisible output tokens. `low` is the lever for
routine work; `grok-4.20-0309-non-reasoning` emits zero reasoning tokens at all
and is the cheap option for anything mechanical.

Measured here: gpt-5.4 defaults to **no** reasoning (0 tokens), while gpt-5 spent
~95% of its output on it. Defaults differ wildly between models in the same
family, so never assume.

---

## Discounts worth knowing

- **xAI batch: 20% off**, but only on grok-4.3 and the grok-4.20-* family.
  **Not grok-4.5.**
- **OpenAI batch: ~50% off.** Anthropic batch: 50% off.
- **Prompt caching is the big one for Zarvis.** Input on grok-4.5 drops
  $2.00 → $0.30, an 85% cut. Our system prompt is ~2,400 identical tokens on
  every call and the review room re-sends the same board to four voices, so
  most of our input is cacheable in principle. Requires a stable
  `prompt_cache_key` (Responses) or `x-grok-conv-id` header (Chat Completions).
  **Not yet implemented.**
- Priority processing is a 2x multiplier and only bills at 2x when the response
  returns `"service_tier": "priority"`. We do not request it.

## Rate limits

xAI tiers unlock permanently on cumulative spend since 2026-01-01: $0 / $50 /
$250 / $1,000 / $5,000. At tier 0, grok-4.5 allows ~150 RPS and 50M TPM, which
is far above anything Zarvis will do. Cached and reasoning tokens both count
toward TPM.

## Sources

- <https://x.ai/docs/developers/pricing.md>
- <https://x.ai/docs/developers/models.md>
- <https://x.ai/docs/developers/model-capabilities/text/reasoning>
- <https://docs.x.ai/docs/key-information/consumption-and-rate-limits>
- <https://developers.openai.com/api/docs/pricing>
- <https://platform.claude.com/docs/en/about-claude/pricing.md>
