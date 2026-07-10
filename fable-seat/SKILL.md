---
name: fable-seat
description: >-
  Operating profile for a Claude Code session whose model is Fable
  (`claude-fable-5`) — the dials that make a Fable-led session run cheaply.
  Effort defaults to `high` and steps DOWN for routine work (not `xhigh` by
  reflex); task framing leads with the outcome instead of prior-model
  step-by-step scaffolding; the prompt cache stays warm by farming cheap work to
  a subagent rather than inline-switching the main loop's model; long runs get a
  task budget plus context compaction; and durable state lives on the memory
  surface. Apply ONLY when the session model is Fable — it is a genuine NO-OP on
  Opus and every non-Fable model. This is Fable-mechanics guidance, NOT a
  superiority claim (it does not assert Fable beats Opus). Use it when you're
  running Fable and want to set the operating dials, before kicking off a long
  agentic run on Fable, or when asked "how should I run Fable", "Fable operating
  profile", "Fable seat", "calibrate for Fable", "am I running Fable
  cost-efficiently".
metadata:
  type: reference
---

# fable-seat

**When the session model is Fable (`claude-fable-5`), apply this operating profile. On Opus — or any other non-Fable model — this entire profile is a NO-OP: apply none of it.**

Fable is priced above Opus, and its mechanics reward different defaults than the models before it. This profile is the set of dials that make a Fable-led session run *cheaply* — nothing more. **It is not a superiority claim.** It does not assert Fable beats Opus; the standing no-measured-edge prior holds. The only claim here is narrow and conditional: *if* you're in the Fable seat, here's how to run it without paying for reflexes tuned to older models.

## Gate first — is this session Fable?

Check the session model (stated in your environment / system context; the exact id is `claude-fable-5`).

- **Not Fable** (Opus, Sonnet, Haiku, anything else) **→ STOP here.** This profile does not apply. Do not step effort down, do not de-prescribe your task framing, do not change caching behavior on account of this profile. It is a genuine no-op — the session runs exactly as it would if this file didn't exist.
- **Fable → apply the dials below.**

This gate is the whole safety of the profile. Every dial that follows is Fable-mechanics-specific, so applying one to a different model is a regression, not a neutral act.

## The dials

### 1. Effort: default `high`, step DOWN — not `xhigh` by reflex

Fable inverts the old instinct. Its low/medium effort often clears what prior models needed `xhigh` to reach, and pushing effort *up* on routine work makes it over-gather and over-deliberate — you pay more tokens for worse-shaped output. So:

- **Default to `high`** for genuinely hard reasoning.
- **Step DOWN to `medium` / `low`** for routine judgment, mechanical edits, and well-scoped lookups. Most work lives here.
- **Reserve `xhigh` / `max`** for the hardest correctness-critical calls only — a subtle proof, a security-sensitive decision, a gnarly root-cause. Reaching for `xhigh` by default is the exact anti-pattern this dial exists to break.

### 2. De-prescribe the seat's OWN task framing — lead with the outcome

Fable does better with *goal + constraints + the outcome you want*, and worse when handed step-by-step scaffolding written for prior models — spelling out the steps drops its output quality. So when you frame a task — for yourself, or in the prompt you write for a subagent / child session:

- **State the goal and the hard constraints, then say "lead with the outcome."** Let Fable choose the path.
- **Don't pre-chew the steps.** The detailed how-to scaffolding that helped older models is a tax here.

**Scope guard — this de-prescription is ONLY for the seat's own task framing.** It does NOT license stripping any skill's safety rules, verification gates, confirmation gates, or the hard walls. Those stay verbatim. You are loosening the *how-to scaffolding you author*, never a guardrail a skill enforces.

### 3. Cache discipline — farm out, never inline-switch the main model

Prompt caching is a prefix match: the cached prefix is reused only while the system prompt and the leading context stay byte-identical. **Switching the main loop's model mid-session — an inline `model=` swap — and back invalidates the whole cached prefix**, so you re-bill the context on both switches. So:

- **Keep the main loop's system prompt frozen and the tool set stable.** Don't churn either mid-session.
- **Route cheaper work to a subagent / child session**, not to an inline model swap on the main loop. A child runs its own (cheaper) model without touching the main loop's cached prefix.

### 4. Task budget on long agentic runs

On a long, many-step agentic run, set a task / token budget so the run has a ceiling and can't quietly balloon. This is the cost guard for runs whose length you can't fully predict up front — pair it with dial 5 on the sessions that also run long.

### 5. Context editing / compaction on long sessions

Fable's context window is large, which means that left alone, a long session fills it with stale tool output and superseded turns that get re-billed on every step. On long sessions, **use context editing / compaction** to drop spent content, so you're not paying to re-read history that no longer matters.

### 6. Point at the durable memory surface

Fable re-derives less when it can read state instead of reconstructing it. Point it at the durable surfaces:

- the **memory / notes file** for facts that outlive the turn, and
- a per-task **`why`** — the goal + constraints — it can re-read instead of re-inferring.

Writing the durable thing down once beats paying Fable to reconstruct it every step.

## What this profile never touches

- **Safety rules, verification gates, confirmation gates, and the hard walls stay verbatim** — on Fable and on every other model. Dial 2 loosens only the task-framing scaffolding you write, never a guardrail a skill enforces.
- **No model comparison, no superiority claim.** If you catch yourself writing "Fable is better / worse than Opus at X," cut it — that's outside this profile's scope and against the standing no-edge prior.

## One-line no-op restatement

Not on Fable? None of the above applies — run the session exactly as you otherwise would.
