# MCP & ToolSearch economy {#mcp-economy}

> Shared `bdaya-core` doctrine reference. Deep home for the constitution's `#router`
> MCP-economy row. Normative keywords follow BCP 14 (RFC 2119, RFC 8174).

## Discipline (from the constitution)

Claude Code defers MCP tool schemas by default; ToolSearch returns the matching schema on demand (~85% fewer tokens).

- Keep `ENABLE_TOOL_SEARCH` at its default. If ToolSearch misses a tool, fix the tool's *description* — you MUST NOT disable the mechanism.
- `alwaysLoad: true` on ≤5 daily-driver servers only (socraticode, filesystem); else defer.
- Add `permissions.deny` globs for context-irrelevant servers (`"mcp__figma__*"`, `"mcp__google-sheets__*"`, `"mcp__discord__*"`) per task.
- `head_limit`/`limit` on every variable-length call (Grep, Read, trees, diffs, k8s dumps) — you MUST pass an explicit small value; default is 250.
- Curate the fleet per task: ≤10 servers via `enabledMcpjsonServers`/`disabledMcpjsonServers`.
- A silent SocratiCode-first nudge is not proof it's down. Before falling back to grep for a LOCATING query, you MUST verify live with one real `codebase_search`.

## MCP Context Economy — full guide

Practical guide to keeping Claude Code's ~40-server MCP fleet context-cheap.
Covers the six active levers: ToolSearch deferral, `alwaysLoad` curation,
`permissions.deny` globs, `ENABLE_TOOL_SEARCH`, `MAX_MCP_OUTPUT_TOKENS`,
and `enabledMcpjsonServers` / `disabledMcpjsonServers` session governance.

Sources verified against `code.claude.com/docs/en/mcp` and
`code.claude.com/docs/en/settings` on 2026-06-27.

---

## 1. ToolSearch deferral — how it works

Claude Code defers MCP tool schemas by default (since v2.1.7 for MCP tools,
v2.1.69 for system tools). At context-load time only tool _names_ are injected.
When the model needs a tool, it queries ToolSearch with a natural-language
description; the harness returns matching tool schemas on demand. Anthropic
engineering research reports approximately 85% token reduction with equal or
higher task accuracy.

**Sources:**
- `code.claude.com/docs/en/mcp#scale-with-mcp-tool-search`

**The mechanism is on by default.** ToolSearch is available to the model via
the deferred-tools system reminder. When ToolSearch misses a tool, the correct
fix is to improve that tool's description to make it a better semantic match
for the queries that should find it. Disabling ToolSearch is never the correct
fix.

---

## 2. `alwaysLoad: true` — daily-driver opt-in

Some tools are referenced in nearly every session; a ToolSearch round-trip adds
friction for them (especially socraticode, given how often it's called). For
these, set `alwaysLoad: true` in the server config — the full schema injects
immediately, bypassing ToolSearch deferral for that server only.

**Config key path (verified at `code.claude.com/docs/en/mcp`, v2.1.121+):**
- `.mcp.json` (Claude Code native `mcpServers` shape): `mcpServers.NAME.alwaysLoad`
- `.vscode/mcp.json` (VS Code / project shared `servers` shape): `servers.NAME.alwaysLoad`

The `alwaysLoad` field is available on all server types (stdio, http, ws, sse)
and requires Claude Code v2.1.121 or later.

**Example — `.mcp.json` (Claude Code native, `mcpServers` shape):**

```json
{
  "mcpServers": {
    "socraticode": {
      "type": "http",
      "url": "https://socraticode.bdaya-dev.com/mcp",
      "alwaysLoad": true,
      "oauth": {
        "clientId": "a3e204b9f1cdf0ab1cbafce9acfad833ae1291cc3cb4479ecac4e321c6fd80e2",
        "callbackPort": 8723
      }
    },
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${CLAUDE_PROJECT_DIR}"],
      "alwaysLoad": true
    }
  }
}
```

**Example — `.vscode/mcp.json` (VS Code / project shared, `servers` shape):**

```json
{
  "servers": {
    "socraticode": {
      "type": "http",
      "url": "https://socraticode.bdaya-dev.com/mcp",
      "alwaysLoad": true
    }
  }
}
```

### Recommended `alwaysLoad` servers for Bdaya DevOps

| Server | Reason |
|---|---|
| `socraticode` | Semantic code search — called in nearly every session, making ToolSearch deferral costly |
| `filesystem` | Directory reads and file access triggered in most sessions |
| `github` | Only for sessions touching `infra-github`; omit for pure client-stack work |

All other servers (kubernetes, argocd-*, browsermcp, penpot, jenkins, flutter-ultra,
discord, figma, google-sheets, mongodb, gcp-cost) should defer. ToolSearch will
find them when needed.

**Limit: never exceed 5 `alwaysLoad` servers.** The preflight banner flags more
than 5 as a `recommended` warning (`mcp_always_load` probe).

**Note on self-protective files:** the plugin's own `.mcp.json` is
self-protective-adjacent (`hooks/**` carve-out), so a change to it warrants extra scrutiny in
review. There is no human-approval carve-out: an independently reviewed PASS at the head is
landed by the implementer like any other change (the `Merge-authority: human` reservation was
removed, shared/claude-plugins#491). The examples above are proposals — copy-paste into your project's
`.vscode/mcp.json` or local `.mcp.json` (not tracked in the plugin repo itself).

---

## 3. `permissions.deny` globs — per-context silencing

`permissions.deny` in `.claude/settings.json` accepts MCP tool glob patterns.
A denied tool is excluded from ToolSearch results — zero tokens spent on it.

**Pattern format (verify at `code.claude.com/docs/en/settings`):**
`mcp__<server-name>__<tool-name-or-glob>`

Note: plugin-bundled server names include the plugin prefix, e.g.
`mcp__plugin_figma_figma__*`.

**For pure DevOps sessions (IaC, GitLab CI, K8s) — add to `.claude/settings.json`:**

```json
{
  "permissions": {
    "deny": [
      "mcp__figma__*",
      "mcp__google-sheets__*",
      "mcp__discord__*",
      "mcp__plugin_figma_figma__*",
      "mcp__plugin_flutter_flutter-ultra-browser__*",
      "mcp__plugin_flutter_flutter-ultra-patrol__*",
      "mcp__plugin_flutter_flutter-ultra-runtime__*",
      "mcp__plugin_flutter_flutter-ultra-native-mobile__*",
      "mcp__plugin_flutter_flutter-ultra-devtools__*"
    ]
  }
}
```

**For Flutter / frontend sessions — add to `.claude/settings.json`:**

```json
{
  "permissions": {
    "deny": [
      "mcp__mongodb-mcp-server__*",
      "mcp__gcp-cost__*",
      "mcp__google-sheets__*"
    ]
  }
}
```

Remove deny entries when switching task types (or use
`disabledMcpjsonServers` for session-scoped control without editing committed
config files).

---

## 4. `ENABLE_TOOL_SEARCH` — controlling deferral

Verified against `code.claude.com/docs/en/mcp#configure-tool-search`.

| Value | Behavior |
|---|---|
| (unset) | **Full deferral mode (default):** all MCP tool schemas deferred, fetched on demand via ToolSearch. Disabled by default on Vertex AI and when `ANTHROPIC_BASE_URL` points to a non-first-party host. |
| `true` | Same as unset on direct Anthropic API. On Vertex AI or custom `ANTHROPIC_BASE_URL`, explicitly re-enables tool search if the proxy forwards `tool_reference` blocks. |
| `auto` | Threshold mode: schemas load upfront if they fit within 10% of the context window, deferred otherwise |
| `auto:N` | Custom threshold percentage, where N is 0-100. Example: `auto:5` for a 5% threshold |
| `false` | **All MCP schemas loaded upfront unconditionally. Never use this.** |

The preflight banner flags `ENABLE_TOOL_SEARCH=false` as a `recommended` warning.

Set in `.claude/settings.json` (persists across restarts):

```json
{
  "env": {
    "ENABLE_TOOL_SEARCH": "auto"
  }
}
```

---

## 5. `MAX_MCP_OUTPUT_TOKENS` — output ceiling

Large MCP tool responses (filesystem directory tree, GitHub PR diff, full
Kubernetes resource dump) can flood the context window. Cap response size with
`MAX_MCP_OUTPUT_TOKENS`.

**Default: 10,000 tokens** (Claude Code displays a warning when output exceeds
this threshold). Source: `code.claude.com/docs/en/mcp` — verified 2026-06-27.

Set in `.claude/settings.json`:

```json
{
  "env": {
    "MAX_MCP_OUTPUT_TOKENS": "10000"
  }
}
```

Within the token ceiling, use per-call `head_limit` / `limit` parameters
(e.g. `Grep head_limit: 50`, `Read limit: 200`,
`mcp__filesystem__list_directory head_limit: 100`). Per-call limits are
cheaper than a session-wide ceiling because they act before the response is
transmitted.

---

## 6. `enabledMcpjsonServers` / `disabledMcpjsonServers` — session governance

These settings keys (in `.claude/settings.json`) let you restrict the active
MCP fleet for a session without editing committed config files. Verified format:
array of plain server name strings (as defined in `.mcp.json`).
Source: `code.claude.com/docs/en/settings`.

**Approve specific `.mcp.json` servers only:**

```json
{
  "enabledMcpjsonServers": ["socraticode", "filesystem"]
}
```

**Reject specific `.mcp.json` servers:**

```json
{
  "disabledMcpjsonServers": ["figma", "google-sheets", "discord"]
}
```

For session-scoped (non-committed) narrowing, copy `.claude/settings.json` to
`.claude/settings.local.json` (gitignored) and set there.

---

## 7. Tool description hygiene

ToolSearch matches against tool descriptions using natural-language semantic
search. Poor descriptions cause missed finds — and the temptation to set
`alwaysLoad` or disable ToolSearch. Fix the description instead.

- Write descriptions that answer the ToolSearch query: "Use tool X when Y."
- Include key domain nouns and verbs the caller would naturally search for.
  (e.g. "kubernetes", "argocd sync", "gRPC decode", "semantic code search")
- Avoid using only the internal function name without context.

The plugin's own MCP tools follow this convention; use them as a reference.

---

## 8. Preflight checks

The bdaya-defaults SessionStart banner runs three MCP-economy checks:

| Probe ID | Severity | Condition flagged |
|---|---|---|
| `tool_search_enabled` | recommended | `ENABLE_TOOL_SEARCH=false` |
| `mcp_always_load` | recommended | More than 5 servers with `alwaysLoad:true` |
| `mcp_fleet_size` | optional | More than 15 total servers in `.mcp.json` + `.vscode/mcp.json` |

Run `/bdaya-bootstrap` to re-run the preflight sweep on demand.

---

## 9. Quick-reference checklist for long task sessions

- [ ] `ENABLE_TOOL_SEARCH` is not set to `false`
- [ ] `alwaysLoad: true` on 3-5 servers only (socraticode + filesystem baseline for DevOps)
- [ ] `permissions.deny` includes context-irrelevant servers for current task type
- [ ] Tools returning variable-length output: explicit `head_limit` / `limit` on each call
- [ ] `MAX_MCP_OUTPUT_TOKENS` set if session will fetch large resources (GitHub diffs, K8s dumps)
- [ ] Preflight banner shows no `mcp_*` warnings (PASS or warnings only on optional probes)

---

## 10. See also — comparative notes vs. other MCP toolsets

- **LSP/AST tools (find-references, diagnostics):** diagnostics via the bdaya-lsp server
  (`mcp__plugin_bdaya-defaults_lsp__lsp_diagnostics{,_directory}` — the OMC port, decision
  D20), references via the native `LSP` tool (`findReferences`) — see
  `references/socraticode-playbook.md` § "Code-intelligence companions". SocratiCode stays
  the FIRST-CHOICE tool for semantic "where/how" questions; the precision layer is for
  exhaustive, tool-guaranteed reference/diagnostics coverage that embedding search cannot
  promise.
- **Typed accountability layer** (`mandate_*` / `decision_*` / `ledger_*`): a strength this
  plugin's MCP server has that OMC's `merge_readiness_*` does not — see
  `references/accountability.md` § "Accountability layer — no OMC equivalent".
