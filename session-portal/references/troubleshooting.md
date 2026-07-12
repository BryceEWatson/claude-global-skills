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

A `delivery_refused` event explains the reason (a held lease, or a steering message with no
`accept-steering` grant on the destination). Delivery is pull-only: the recipient drains its
own inbox through the MCP tool `portal_list_inbox {deliver: true}` (acting as its bound
token). For an operator-driven drain, vouch for the boundary explicitly:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" inbox --session <product:id> --deliver --at-boundary
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

## An identity-bearing tool returns `authorization_error`

The MCP server needs a valid token to act. Mint one and put it in the server's env
(`SESSION_PORTAL_TOKEN`); see `mcp-config.md`. A token that expired or was revoked also fails
— re-issue it:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" issue-principal --product claude --session <id>
python "{{SKILL_HOME}}/scripts/portal_admin.py" list-principals
```

## A steering message won't deliver

Steering needs two operator grants: `send-steer` on the sender and `accept-steering` on the
destination (scoped to the counterparty). Check and grant:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" list-grants
python "{{SKILL_HOME}}/scripts/portal_admin.py" grant --to <dest> --capability accept-steering --scope <sender> --ttl 3600
```

## Codex delivery says "queued", never "delivered"

That is expected: delivery is pull-only. A message to a Codex session becomes delivered when
that Codex session pulls its own inbox (as its bound token). There is no native-task push in
this MVP; `codex-status` always reports native messaging as not an implemented delivery
channel:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" codex-status
```

## Uninstall / rollback

Remove the database (reversible — the next command recreates an empty one):

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" uninstall --yes
```

To remove the skill's installed files, re-run the repo's sync engine or delete the
installed skill directory under the product's skills home. The repo remains the source of
truth, so a redeploy restores everything.
