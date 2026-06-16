---
name: session-handoff
description: Alias of `session-end` (this skill was renamed). Ending / closing out a session — and handing it off when work is mid-flight — now lives in session-end; "handoff" is its mid-flight mode. Invoking this just routes to session-end so old muscle memory ("/session-handoff") keeps working.
allowed-tools: Bash, Read, Grep, Glob, Write, TodoWrite
---

# session-handoff (alias → session-end)

This skill was **renamed to `session-end`**. Ending a session is the umbrella intent (always produce the
evidence-grounded record); *handoff* is the mid-flight mode that also emits a resume prompt.

**Do this now:** invoke the **`session-end`** skill (via the Skill tool) and follow it exactly. Do not
re-implement or duplicate its logic here — it is the single source of truth for closing out a session.
