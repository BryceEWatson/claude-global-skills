# Session portal — MCP configuration

## In plain terms

The portal runs as a small local program that each assistant talks to over its standard
input and output (an "MCP server"). This page shows how to register that program with
Claude Code and with Codex so their tools appear. The command path below uses a
placeholder that expands to the right install location for whichever product you are
configuring.

## The command

The server is launched with:

```bash
python "{{SKILL_HOME}}/scripts/portal_mcp.py"
```

`{{SKILL_HOME}}` expands at install time to this skill's directory inside the product you
deployed to (Claude Code or Codex). It speaks JSON-RPC 2.0 over stdio and binds nothing to
the network.

## Claude Code

Add the server to your MCP configuration (project `.mcp.json` or the user scope). Example
`.mcp.json`:

```json
{
  "mcpServers": {
    "session-portal": {
      "command": "python",
      "args": ["{{SKILL_HOME}}/scripts/portal_mcp.py"]
    }
  }
}
```

The tools then appear as `portal_list_sessions`, `portal_get_session`,
`portal_send_message`, `portal_list_inbox`, `portal_acknowledge`, `portal_cancel_message`,
`portal_get_message_status`, plus `portal_register_session`, `portal_message_events`, and
`portal_health`.

## Codex

Register the same stdio command with Codex's MCP configuration (the Codex install carries
the interface manifest at `agents/openai.yaml`):

```toml
[mcp_servers.session-portal]
command = "python"
args = ["{{SKILL_HOME}}/scripts/portal_mcp.py"]
```

## Verifying

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" health
```

A healthy response reports the schema version, the database path, message counts by
status, and the number of active leases.

## Notes

- The exact config file and key names follow each product's own MCP conventions; the
  command (a stdio launch of `portal_mcp.py`) is what matters.
- The database location is shared across both products so a message sent from one is
  visible to the other. Override it with `SESSION_PORTAL_HOME` (directory) or
  `SESSION_PORTAL_DB` (exact file) if you need an isolated instance.
