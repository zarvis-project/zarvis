# Voice: TEMPLATE

Copy this to `prompts/voice.md` and rewrite it as yourself. `voice.md` is
gitignored, so your version stays yours.

**Do not ship this file as-is.** It describes a generic register, and drafts
written against a generic register read like a model wrote them, because one
did. The whole point of this document is the part only you can supply.

Pairs with `humanize.md`, the mechanical floor.

---

## The register

One or two sentences on who you sound like. Not adjectives, a person.

> *Example: "A founder who has done the work and is talking to a peer. Warm, not
> eager. Assumes the reader is busy and competent."*

## Traits

Six to ten. Concrete and checkable, not aspirational.

- **How you open.** *Hey,* / *Hi X,* / straight into the sentence, no greeting.
- **Contractions?** Most people use them and then write email like a contract.
- **Sentence length.** Do you vary it, or is everything short?
- **What you do with numbers.** Cite them, round them, avoid them?
- **How you ask.** A question at the end, an offer, or a statement that leaves
  space.
- **How you close.** The exact signoff, and whether the name has a period.

## The part that matters most: what you never write

This does more work than everything above it combined. Models reach for the same
handful of tells, and naming them is what stops the output sounding generated.

Start here and add to it every time you edit a draft because a phrase made you
wince:

- Never: *I hope this email finds you well* · *I wanted to reach out* ·
  *Just circling back* · *touching base* · *synergy* · *leverage* as a verb ·
  *excited to announce* · *quick question* as an opener when it is not quick.
- Never: em dashes. Commas and periods. (This one is a hard rule in
  `lint.py`, because it survived every prompt instruction until it was enforced
  in code.)
- Never: promising a duration you did not agree to. *"A 20-minute walkthrough"*
  is a commitment. *"Got 15 minutes?"* is a question. The linter blocks the
  first and allows the second.

## Real examples

Paste four or five lines from your own sent mail. Not rewritten, not tidied.
This section is worth more than the whole document above it, because it is
evidence rather than description.

> *"..."*
> *"..."*

## Register conflict

If this file and `humanize.md` disagree, say here which wins and when. They will
disagree. One is about sounding like a person and the other is about sounding
like *you*, and those pull apart at the edges.
