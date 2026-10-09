---
name: email-editorial-pass
description: >-
  Run brycewatson.com's editorial voice pass over any outbound email BEFORE the draft is created. Fires on drafting, replying to, or composing any external / client / lead email on Bryce's behalf, including any create_draft of mail he sends. Outbound email clears the same voice bar as published content: no em dashes, no hype, first person + contractions, front-loaded point. Also holds Bryce's outbound habits: a context pack instead of a draft unless he asks for the words, and email stays a draft.
user-invocable: true
---

# Outbound email editorial pass

Every external email Bryce sends represents him the same way a published post does, so it clears the **same voice bar** as brycewatson.com content. Step 1 decides first whether a draft is wanted at all; when it is, the rest of the pass runs on it **before** the Gmail draft is created. No outbound email skips it, even a one-liner (the pass is fast). Sending stays Bryce's action; this governs what gets prepared, what the draft says and how it's shown to him.

## Source of truth (read it; do not fork the rules)

In priority order:
1. `<brycewatson.com checkout>\_planning\voice-profile.md` — the living voice spec, the full source of truth.
2. `<brycewatson.com checkout>\.claude\agents\editorial-reviewer\agent.md` — the reviewer's exact flagging rules and severities.

These live in the brycewatson.com repo (read-only from here). If the checkout is missing, fall back to the inline hard rules below — they are the stable subset, not the whole spec.

The **outbound habits** in step 3 are the one part this skill owns. They're Bryce's rules for what gets prepared and how it's framed, not for how the prose sounds, so the voice profile isn't their home. Apply them every time, whether or not you could read the checkout.

## The pass

1. **Decide: context pack or draft.** An email or note from Bryce to a person (a client, a lead, a contact) defaults to a **context pack**, not a draft: everything Bryce needs to know while he writes it himself (who it's to, where the thread stands, the point to land, the facts and links he'll want), with no wording and no subject line. Write the words only when he asks for them ("draft a reply to Sam"). A PR or issue body, or a status update a session posts as part of its own work, isn't a message he'd write himself, so it takes the draft path.
   - **Context pack:** skip steps 2, 4 and 5. A pack carries none of his voice, so of the step 3 checks only "The work belongs to the project" applies; fix what it flags in the pack, then go straight to step 6. No draft is created on this path.
   - **Draft** (he asked for the words, or it's a PR, issue or status body): **draft** it normally and carry on through steps 2 to 6.
2. **Read** the voice-profile. Apply the hard rules + register to email. Do **not** force blog-only rules (keyword/SEO titles, the essayist "professional moves", one-quotable-line-per-post) onto a short email.
3. **Review** the draft and flag every hit. For a substantial / high-stakes email, dispatch the brycewatson.com `editorial-reviewer` lens as a subagent for an independent pass; for a short reply, apply the checklist inline (that is the lens). The reviewer lens covers voice only, so check the outbound habits yourself either way.

   **Outbound habits** (owned here; check all four on every draft, and only the project one on a pack):
   - **[MUST FIX] Open with the thing.** The first line is the point or the ask. No name greeting ("Hi Sam,"), and no courtesy opener or throat-clear ahead of the point.
   - **[MUST FIX] Never "waiting on Bryce".** Say what happens next instead.
   - **[MUST FIX] The work belongs to the project.** Name the project it's part of. Never frame it as done "for" a person.
   - **[MUST FIX] An invoice states what's billed, nothing else.** In an invoice, or the email that carries one: the billed items and amounts. No recap of the work, no pitch, no commentary.

   **Voice** (the fallback subset of the voice profile):
   - **[MUST FIX] Em dashes.** No `—`, no `–` (en dash), no ` -- `. **Check the subject line too.** Recast keeping the scope-and-qualify move: a colon, a parenthetical, or a second sentence. Do not just delete the dash.
   - **[MUST FIX] Hype / manufactured drama.** No "game-changing", "revolutionary", "seamless", "unleash", "dive in", "thrilled / excited to", suspense-builders ("the key insight", "here's the thing"). Plain and concrete.
   - **[MUST FIX] De-personalized / passive register.** First person, active voice, contractions are mandatory. No report-prose, no passive nominalizations ("Analysis revealed…").
   - **[SHOULD FIX] Plain-text artifacts.** Drop `*emphasis*` asterisks and other markdown that renders literally in a plain-text email.
4. **Revise** — apply every MUST FIX and the worthwhile SHOULD FIX.
5. **Create the draft** (draft path only) after the pass is clean. **Email stays a draft:** never send it; the send is Bryce's. **Avoid bare URLs in the body:** Gmail auto-links them into a `google.com/url?...` redirect wrapper that can bake into the saved draft as literal text. Write a domain as plain text with no scheme, or add the link inside Gmail's compose. See [[verify-rendered-output-before-claiming]].
6. **Report** to Bryce. **Link first when there is one:** the issue that tracks it or its Gmail thread. New outreach may have neither; then leave the link out rather than invent one.
   - **For a draft:** then the draft as it reads, then one line on what the pass changed (e.g. "removed 2 em dashes, cut one hype word, dropped markdown asterisks"). If you say the draft was created, you actually called create_draft; verify, don't claim.
   - **For a context pack:** then the pack, and say plainly that no draft was created.

## Notes

- This is the **global outbound-email gate**. The voice rules are **owned by brycewatson.com**; read them, don't duplicate them here (the inline list is a fallback subset only).
- The **outbound habits** are the exception, and they live here on purpose. They decide what gets prepared (a pack or a draft) and how it's framed and shown, which is a question of how Bryce works rather than of voice. They were written for the "how Bryce works" block of his global instructions, and that file had no room left for them.
- Canonical copy of this skill belongs in the public `claude-global-skills` repo — PR changes there, and parameterize the absolute paths before publishing.
