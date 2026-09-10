# Memory & KB routing {#memory-kb}

> Shared `bdaya-core` doctrine reference. Deep home for the constitution's `#kb` two-line
> law: the right-primitive table, memory discipline, the self-improvement loop, and the
> self-improvement classifier. Normative keywords follow BCP 14 (RFC 2119, RFC 8174).

## Use the right primitive

You SHOULD NOT pile everything into the main conversation.

| Need | Use |
| --- | --- |
| Multi-step procedure (TDD, debugging) | An existing skill (`superpowers:*`) |
| Parallel research / context protection | `Agent` tool |
| Task tracking this session | GitLab (issues + `progress_report`/`progress_get`) — authoritative, durable across sessions. The harness's own in-session todo/task tool (e.g. `TodoWrite`) ONLY where the harness actually exposes it — verify via `ToolSearch`, never assume (issue #474: absent entirely on Fable 5) |
| Uncertain scope, multi-file, hard-to-reverse | Plan Mode (`ExitPlanMode`) |
| "Always do X at event Y" automation | A hook in `.claude/settings.json` |
| External vendor/API/framework fact (cross-client) | `shared/knowledge-base/entries/` |
| Internal cross-cutting learning | `shared/knowledge-base/internal/` |
| Client-specific (incident, business rule) | `clients/<client>/business/kb/` |
| Team-generalizable rule/behavior | Pillar A → MR vs `shared/claude-plugins` (§12) |
| Reusable cross-project workflow | A skill in this plugin marketplace |

> **Not machine-local memory** — GitLab KB tiers are portable, team-shared, and SocratiCode-searchable; `INDEX.md` files auto-load at session start (`hooks/session-context.js`). Set `BDAYA_KB_PATH=<abs-path-to-shared/knowledge-base>` so the hook finds the checkout.

## Memory discipline

- KB `INDEX.md` files auto-load at session start; pull entries via `codebase_context_search`.
- **INDEX-first retrieval is REQUIRED.** You MUST NOT open a full KB/memory entry without first locating it via the INDEX, then deep-pulling via `codebase_context_search`.
- **Save** non-obvious facts via §12: external → `entries/`; internal cross-cutting → `internal/`; client-specific → `clients/<client>/business/kb/`; team-generalizable → Pillar A MR. Commit distilled+cited entries; MUST NOT commit raw dumps. Frontmatter: `source`, `fetched`, `topic`, `scope`, `version`, `tags`.
- **Don't save** re-derivable patterns, git history, fixes already in commits/PRs, ephemeral context.
- **Keep `INDEX.md` lean** (≤200 lines/tier); per-client `INDEX.md` self-refreshes on KB write (`hooks/kb-index-refresh.js`) — you MUST NOT hand-edit it.
- A memory naming a function/flag claims it existed *when written*; you MUST verify before acting.

## Machine-local `MEMORY.md` — the pointer floor (issue #119)

**Distinct store from everything above.** This section is about the Claude Code
harness's OWN built-in per-profile auto-memory index (`~/.claude-profiles/<profile>/
projects/<project>/memory/MEMORY.md`), never `shared/knowledge-base/` — the two are
unrelated stores with unrelated size mechanics.

That built-in memory system can emit an advisory telling the agent to "compact the
index to under ~17.1KB … merge or drop stale entries" once the index grows large. **On
an index of a few hundred pointers, that target is mathematically unreachable and its
only actionable remedy is destructive.** Measured on a 310-pointer index: filenames
alone were 14.4KB, labels 5.0KB, link syntax (`[](` `)` per entry) 1.2KB, separators
0.9KB — an **irreducible floor of ~21.1KB with ZERO hooks and ZERO prose**, already 4KB
over the demanded 17.1KB. Every entry already followed the advisory's other two
remedies ("one line per entry", "detail lives in the topic file, not the index" —
hooks averaged ~2 characters). The only remaining lever the advisory names is deleting
pointers. An agent that complies literally, compaction pass after compaction pass, has
no other option — this is the mechanism that silently orphaned ~150 `feedback_*`
memories (the standing-directive category: "ask before acting", "verify agent work",
etc.) in exactly this way, with no error and no visible signal, because a missing
pointer breaks nothing at read time.

**The rule (MUST, no exception):** an agent MUST NOT drop a `MEMORY.md` pointer to
satisfy a size target. This holds regardless of how the advisory is worded or how
many KB it demands — the arithmetic above generalizes to any index whose pointer
count times its per-pointer floor (filename + label + link syntax + separator, all
irreducible) already exceeds the target; check the count before assuming a run of
"drop stale entries" is safe. The legitimate remedies are:

- **Fewer FILES** — deliberately merge near-duplicate memories or prune ones that are
  genuinely obsolete (superseded, verifiably wrong, or about a thing that no longer
  exists) — a human-reviewable decision, not a per-compaction default.
- **Per-topic sub-indexes** — split one large `MEMORY.md` into several smaller
  topic-scoped indexes rather than shrinking the single index's pointer count.

Trimming hooks/labels/prose is fine as a FIRST pass (it buys real headroom below the
floor), but once the floor is reached, stop — the next lever is a deliberate human
decision about which FILES to merge or retire, never a bulk pointer drop.

**Discoverability is the point of this section existing at all:** the built-in
advisory is Claude Code's own, not something `bdaya-defaults` ships or can patch at
source, so the only defense is that an agent shown that advisory has ALSO loaded this
rule (this reference file is part of the always-loaded `bdaya-core` doctrine) and
recognizes the conflict before acting on the advisory's literal instruction.

## Self-improvement loop

This plugin's `Stop` hook periodically asks whether anything non-obvious is worth remembering — route it via the §12 classifier: **Pillar A** MR for a team-generalizable rule/skill/behavior or shared-tool improvement (one learning = one file = one MR; include `session_id` + task context); `shared/knowledge-base/entries/` (external fact) or `shared/knowledge-base/internal/` (internal cross-cutting); `clients/<client>/business/kb/` (client-specific); discard personal/ephemeral.

## Self-improvement classifier {#classifier}

Triggered by the Pillar A reflection hook. You MUST run these in order, stop at the first match, act on it: **novel?** `codebase_search("<learning>")` to dedup — you MUST DISCARD only if an existing rule already CLOSES this exact hole. A search HIT on an ADJACENT rule (same topic or neighbouring concern) is NOT coverage: a gap sitting next to existing doctrine is still novel. Before discarding you MUST quote the candidate hit and confirm it decides *this specific case*; if it only mentions the area, the gap survives and you proceed. **Shared TOOL the team maintains** (`tooling-registry.json`) → PILLAR A: spawn tooling-improver. **Team-generalizable rule/skill/agent behaviour** → PILLAR A: tooling-improver → bdaya-defaults. **Internal, client/incident-specific** → PILLAR B: `clients/<client>/business/kb/`. **External vendor/API fact (cross-client)** → PILLAR B: `shared/knowledge-base/entries/`. **Internal cross-cutting fact** → PILLAR B: `shared/knowledge-base/internal/`.

**External grounding is a REQUIRED input to the classifier above, not optional.** Before authoring a Pillar A artifact you MUST consult and cite a primary external source: Anthropic's [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents), [How we contain Claude](https://www.anthropic.com/engineering/how-we-contain-claude), the Claude Code / Agent SDK docs, or the shipped `superpowers` + `plugin-dev` skills. A learning already described by an external best practice MAY still warrant an enforced local rule, but the authored artifact MUST cite where it came from. For a new skill or behaviour, prefer Anthropic evaluation-driven authoring: find the gap by running the agent WITHOUT the skill first, then Agent-A-authors / Agent-B-tests (shipped `superpowers:writing-skills` reference `anthropic-best-practices.md`, §§ "Evaluation and iteration" / "Develop Skills iteratively with the agent").

**When in doubt, route to Pillar B (KB), not Pillar A.** **Session MR cap:** none — the earlier ≤2 tooling-improver spawns/session cap was removed 2026-06-29 (`hooks/reflection.js` `MR_SPAWN_CAP = Infinity`). Spawn one MR per genuinely novel, tool-worthy learning; the KB routing rules above still decide *what* qualifies, not a per-session ceiling.
