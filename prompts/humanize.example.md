# Copy rules: humanization

Copy this to `prompts/humanize.md`. Unlike `voice.md`, most of this is
transferable and you can probably ship it close to as-is. It is the mechanical
floor: the tells that make copy read as machine-written regardless of whose
voice it is wearing.

`voice.md` says who you sound like. This says what nobody should sound like.

---

## 0. Em dashes are forbidden

No `—`, no `–`. Periods or commas.

This is the rule that will not survive on instruction alone. Models emit em
dashes under any prompt telling them not to, so Zarvis does both: the rule is in
the compose prompt, AND a deterministic sweep replaces them after generation,
AND a surviving em dash is a blocking lint finding. Three layers for one
character, because it is the single most reliable tell there is.

## 1. Cut the fluff closer

Machine copy ends on a sentence that adds nothing. *"Looking forward to hearing
your thoughts!"* · *"Let's chat soon!"* · *"Excited to hear what you think."*
Delete it.

Judgment applies. If the last line carries a real offer or a specific next step,
keep it. The test is whether removing it loses information.

## 2. Resolve dash asides

An aside is a dash followed by a fragment: `—love it!`, `-total game changer!`

- **Short, one to three words:** delete the dash and the fragment.
- **Longer:** make it its own sentence. Drop the dash, capitalize, punctuate.

> *"...noticed your team expansion—exciting to see such growth!"*
> becomes
> *"...noticed your team expansion. Exciting to see such growth!"*

## 3. Transitions only where the flow actually breaks

If two ideas are jammed together, connect them: *"Also,"* · *"By the way,"* ·
*"That got me thinking,"*. Do not add transitions that are not needed, and never
change the ideas themselves.

## 4. No trailing period on the signoff

`Best, Sam` and not `Best, Sam.`

## 5. Dial down enthusiasm

Target register: the expert in the room. Confident, straightforward, no hype.

- Clipped adjectives over effusive ones: *amazing* → *impressed by*,
  *loved* → *intrigued by*.
- Exclamation points become periods wherever they read as gushing.

This is a tone rule, not a ban on warmth. A greeting like *"Hey Sam!"* is voice,
and voice wins.

## 6. Banned vocabulary

Replace with a plainer synonym:

> keen · dive in · delve · supercharge · synergy · shaking up · jazzed · knack ·
> innovation · reckon · pumped · spot on

Add to this list every time you edit a draft because a word made you wince. It
is the highest-yield maintenance in the whole system.

## 7. Minimal intervention

Nothing beyond the rules above. If a draft needs no changes, leave it alone.
Never add a recipient's name that was not already there.

---

## How this is wired

- These are **composition constraints** in the compose prompt, not a second
  model call. One pass, not two.
- Rule 0 also runs as code after generation, before the draft is queued.
- **Do not conflate this with the assertion linter.** The linter blocks claims
  the evidence does not support. This governs register and punctuation. A tone
  edit must never be recorded as a factual correction, and a factual correction
  must never be filed as a style note, or the learning loop learns the wrong
  lesson from every edit you make.
