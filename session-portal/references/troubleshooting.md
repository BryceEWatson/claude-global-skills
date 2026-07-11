# Session portal — troubleshooting, recovery, uninstall

## In plain terms

Short answers for when something looks stuck: how to check the portal is healthy, how to
clear a lock left behind by a crash, how a message expires, and how to remove the portal
cleanly (and put it back). All commands print JSON so you can script them.

## Health check

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" health
```

Reports schema version, DB path, message counts by status, and active leases. Running it
also sweeps expired messages and reclaims any expired locks, so it doubles as a nudge.

## A message is stuck in `queued`

Delivery only happens at a safe boundary, so a queued message is usually just waiting for
the recipient to reach one. Check why:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" events --message <message-id>
```

A `delivery_refused` event explains the reason (recipient `active`, state `unknown`, a held
lease, or an unauthorized steering attempt). Recipients drain their inbox with:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" inbox --session <product:id> --deliver --boundary stop
```

## Stale lock after a crash

Delivery to a destination is serialized by a lease. If a deliverer crashed mid-delivery,
the lease is reclaimed automatically once its TTL (120 s) lapses. To reclaim expired locks
immediately:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" recover-locks
```

This only removes leases whose TTL has already passed; a live lease is left alone.

## Expiring old messages

Messages carry a TTL (default 24 h). To sweep everything past its deadline to `expired`:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" expire
```

## Cancelling a message you sent

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" cancel --message <message-id> --by <your-session>
```

You can cancel a `queued` or `delivered` message; you cannot cancel one that was already
acknowledged, expired, or failed.

## Codex delivery says "queued", never "delivered"

That is expected unless a real Codex task tool is surfaced to the running task. Check:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" codex-status
```

By default native task messaging is reported unavailable and delivery falls back to the
durable queue, which Codex drains at a supported boundary. Only set
`SESSION_PORTAL_CODEX_NATIVE=1` in a context that genuinely surfaces the task tool.

## Uninstall / rollback

Remove the database (reversible — the next command recreates an empty one):

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" uninstall --yes
```

To remove the skill's installed files, re-run the repo's sync engine or delete the
installed skill directory under the product's skills home. The repo remains the source of
truth, so a redeploy restores everything.
