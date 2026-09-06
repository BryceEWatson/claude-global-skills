---
targets: [claude]
name: email-editorial-pass
description: Run brycewatson.com's editorial voice pass over any outbound email BEFORE the draft is created. Fires on drafting, replying to, or composing any external / client / lead email on Bryce's behalf, including any create_draft of mail he sends. Outbound email clears the same voice bar as published content: no em dashes, no hype, first person + contractions, front-loaded point.
user-invocable: true
---

# Outbound email editorial pass

Every external email Bryce sends represents him the same way a published post does, so it clears the **same voice bar** as brycewatson.com content. Run this pass on the draft **before** creating the Gmail draft. No outbound email skips it — even one-liners (the pass is fast). Sending stays Bryce's action; this only governs what the draft says.

## Source of truth (read it; do not fork the rules)

In priority order:
1. `C:\Users\Bryce\Projects\brycewatson.com\_planning\voice-profile.md` — the living voice spec, the full source of truth.
2. `C:\Users\Bryce\Projects\brycewatson.com\.claude\agents\editorial-reviewer\agent.md` — the reviewer's exact flagging rules and severities.

These live in the brycewatson.com repo (read-only from here). If the checkout is missing, fall back to the inline hard rules below — they are the stable subset, not the whole spec.

## The pass

1. **Draft** the email normally.
2. **Read** the voice-profile. Apply the hard rules + register to email. Do **not** force blog-only rules (keyword/SEO titles, the essayist "professional moves", one-quotable-line-per-post) onto a short email.
3. **Review** the draft and flag every hit. For a substantial / high-stakes email, dispatch the brycewatson.com `editorial-reviewer` lens as a subagent for an independent pass; for a short reply, apply the checklist inline (that is the lens):
   - **[MUST FIX] Em dashes.** No `—`, no `–` (en dash), no ` -- `. **Check the subject line too.** Recast keeping the scope-and-qualify move: a colon, a parenthetical, or a second sentence. Do not just delete the dash.
   - **[MUST FIX] Hype / manufactured drama.** No "game-changing", "revolutionary", "seamless", "unleash", "dive in", "thrilled / excited to", suspense-builders ("the key insight", "here's the thing"). Plain and concrete.
   - **[MUST FIX] De-personalized / passive register.** First person, active voice, contractions are mandatory. No report-prose, no passive nominalizations ("Analysis revealed…").
   - **[SHOULD FIX] Front-load.** The point or ask lands early. A courtesy opener ("Thanks for reaching out") is fine; just keep the substance near the top, not after a throat-clear.
   - **[SHOULD FIX] Plain-text artifacts.** Drop `*emphasis*` asterisks and other markdown that renders literally in a plain-text email.
4. **Revise** — apply every MUST FIX and the worthwhile SHOULD FIX.
5. **Create the draft** only after the pass is clean. **Avoid bare URLs in the body** — Gmail auto-links them into a `google.com/url?...` redirect wrapper that can bake into the saved draft as literal text. Write a domain as plain text with no scheme, or add the link inside Gmail's compose. See [[verify-rendered-output-before-claiming]].
6. **Report** to Bryce: draft is ready + one line on what the pass changed (e.g. "removed 2 em dashes, cut one hype word, dropped markdown asterisks"). If you assert the draft was created, you actually called create_draft — verify, don't claim.

## Notes

- This is the **global outbound-email gate**. The voice rules are **owned by brycewatson.com**; read them, don't duplicate them here (the inline list is a fallback subset only).
- Canonical copy of this skill belongs in the public `claude-global-skills` repo — PR changes there, and parameterize the absolute paths before publishing.
