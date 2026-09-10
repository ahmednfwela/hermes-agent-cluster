---
describes:
  - plugins/bdaya-defaults/hooks/mandate-gate.js
verified:
  plugins/bdaya-defaults/hooks/mandate-gate.js: 79cf4222e11328e3
verified_at: 2026-08-04
---

# Merge policy — autonomous-merge gates {#merge-policy}

> Shared `bdaya-core` doctrine reference. Loaded on the merge-gate signature. Deep
> home for the constitution's `#router` "autonomous merge / --sha pin / reviewer-bot" row.
> Normative keywords follow BCP 14 (RFC 2119, RFC 8174).

**The agent stack MUST NOT merge without satisfying every happy-path gate. It MUST NOT skip the ladder routing when a gate fails.** Four gates MUST all hold: (1) CI validate (`validate:plugin` + `test:hooks`) green, no allow_failure override; (3) `agent-gate` exit 0; (4) AI review by `bdaya-reviewer` (fresh context, Opus xhigh), recorded as a verdict **pinned to the exact head SHA** and **stating the model it actually ran on** (`Model:`, enforced against the `opus` tier since #506 — [the tier is now observed](#tier-is-requested-not-observed)). Its mechanical backing is **RV-1** — a sha-pinned fresh-context reviewer PASS note, checked by `mandate-gate`/`validateReviewerVerdict`. **The reviewer-bot native GitLab approval is REQUIRED again — server-side — since the 2026-09-02 owner ruling (shared/claude-plugins#683): every estate GitLab project carries an `any_approver` rule `reviewer-bot` with `approvals_required: 1` and `merge_requests_author_approval: false`, so GitLab itself refuses a merge with no approval.** That approval is minted by `mr_approve` / `approve_and_merge` (the reviewer-bot credential is present on every fleet member, #638/#683 parity), and it is *in addition to* RV-1, never a substitute: `validateReviewerVerdict` still does not read `approved_by` (Option A, 2026-08-18), and a native approval with no sha-pinned verdict note is still the `!337` shape and still denies. See [#approval-required-again](#approval-required-again). The hook-layer coverage of gate 4 is **PARTIALLY LIVE — proven only in armed Bash contexts, absent on most other merge surfaces** — see [gate 4 enforcement belongs in the hook layer](#gate4-hook-enforced) and its STATUS box; (5) loop state: the PASS came from a CONVERGING LADDER-1 loop (`ladder` state in `prd.json` — bdaya-work `references/escalation-ladder.md`); an MR routed to TFR-1 is parked, never merged. (Former gate 2, `protected-paths-gate` exit 0, was removed 2026-07-16 — PM decision, "it duplicates the native Reviewer Bot Approval rule and doesn't add anything useful" — on `shared/claude-plugins` gate 4's native GitLab approval already blocks every MR pending `reviewer-bot` sign-off regardless of which paths it touches, making the path-scoped gate strictly redundant there. The surviving gates keep their historical numbers 1/3/4/5 rather than closing the gap: "Gate 4" is a stable cross-referenced name in this doc's own gate-4-config anchor below, `docs/autonomous-merge-runbook.md`'s own section heading, `config/native-approval.json`, `gitlab/lib/credential.js`, and several test files — renumbering it here would desynchronize this doc from all of those.)

<a id="tier-is-requested-not-observed"></a>
**"Opus xhigh" in gate 4 was a REQUEST that nothing observed. It is now a STATED, ENFORCED field — and a request-side pin was never the thing to fix.** The reviewer MUST state the model it ACTUALLY EXECUTED on as a `Model:` line in its RV-1 verdict note (`agents/bdaya-reviewer.md`, `verdict-schema.md` [#review-model](verdict-schema.md#review-model)), read from the line the harness puts in its own system prompt — *"You are powered by the model named …"*. `validateReviewerVerdict` and the merge-time verdict gate (`mr.js` `assertVerdictAllowsMerge`, which runs on **every** `mr_merge`/`approve_and_merge`, armed or not) both REFUSE a PASS whose stated model ranks below `opus`, and both fail CLOSED on a verdict that states no model at all or one they cannot rank — naming the measured model in the refusal (`shared/claude-plugins#506`). **In practice that second reader is the one you meet**, and it got more load-bearing after `!728` retired the CI reviewer sweep: `mr land --sha <live>` — the implementer's own self-merge, now the only thing that lands an MR — resolves to `approve_and_merge`, so the tier check sits directly on the command an agent types to land its own work, not on a sweep that no longer runs.

**What the measurement actually established, because it changes what may be claimed.** The four reproductions on #506 are real: a `bdaya-reviewer` requested as `opus` executed entirely on `claude-sonnet-5`, 147/147 and 136/136 assistant messages with no mixing, on two different machines. But the 14-cell matrix that followed (#506 note 89313 — macbook, Claude Code 2.1.241, session `claude-opus-5[1m]`) found the OPPOSITE on that host: **every requested tier was the tier that executed**, across both spawn paths (`Workflow` `agent()` and the `Agent` tool), with and without an `agentType`, for `haiku`, `sonnet` and `opus` alike — and an agent-definition FRONTMATTER pin is honoured too (`backend-specialist` pins `model: sonnet`, was given no override, and ran `claude-sonnet-5` from an Opus session). The exact configuration reported as producing Sonnet — `Workflow` → `agentType: bdaya-defaults:bdaya-reviewer`, `model: 'opus'`, `effort: 'xhigh'`, `spawnDepth: 1` — produced `claude-opus-5` there. **So the divergence is environment- or runtime-dependent, not a property of the spawn configuration: there is no spawn path known to drop the pin and no known-good spawn path to prefer.** Do not write "spawn it this way and the tier is guaranteed" — no such instruction is supported by the evidence. Write instead: the tier is whatever the verdict note says it was, because that is the only reading taken after the fact rather than before it.

This was a fresh instance of the class `references/outcome-not-report.md#outcome-not-report` names, applied to gate 4 itself: a gate reported success (a tier was "requested") and the property it exists to certify — review actually performed at the stated capability level — did not happen, with nothing downstream positioned to notice. **A gate that cannot observe the property it certifies is not yet a gate.** That is now closed at the only point that survives an unknown mechanism: the reviewer states its resolved model, and the gate reads it. The `Model:` field is trustworthy for this because it was checked before being built on — in 8/8 cells of the matrix above, the agent's own system-prompt model line matched the model measured independently in its transcript, a `haiku`-executed agent reporting Haiku from an Opus-spawning session. **The manual transcript read-back is therefore no longer the gate, but it remains the AUDIT**: `Model:` is a self-report, so an agent that lies or mis-copies is not caught by the gate — spot-check `message.model` in the reviewer's own `agent-*.jsonl` against the field when a tier claim is load-bearing, and never conflate "the field says opus" with "a transcript was read". A tier divergence does **not** automatically void a review's independence — a fresh-context reviewer that found real defects satisfies D6 (`accountability.md`) whatever it ran on — but it **does** void the tier claim specifically, and the two MUST NOT be merged into one verdict line.

<a id="gate4-hook-enforced"></a>
**Gate 4's enforcement BELONGS in the hook layer, not in GitLab — and does not live there yet.** (PO decision 2026-07-26, superseding the earlier "GitLab-level enforcement is best" call, which was **wrong for this use case**.) This section states the design target and the argument for it. Read the implementation-status box immediately below BEFORE relying on any of it.

> **STATUS: STEP 0 (BUILD) DONE. STEP 1 (LIVE-ARM-AND-PROVE) EXECUTED 2026-08-04 — the
> watched DENY is CONFIRMED ALIVE in the armed Bash contexts, and CONFIRMED ABSENT in
> several contexts that can also merge, so STEP 2 REMAINS FORBIDDEN.** (Issue #57 item 1;
> scratch run-ledger #212; live deny target MR !457; installed plugin 2.33.2; full
> per-context matrix with verbatim evidence in the 2026-08-04 step-1 report note on #57.)
>
> - **Watched LIVE DENY** (unproven merge, active-run pointer armed, cwd = project root):
>   Bash `glab mr merge <iid> -R <proj> --sha <head>` — harness blocked pre-execution with
>   `[bdaya mandate-gate] DENIED … CG-1, RV-1` — in the MAIN session AND in a Task
>   subagent; and the tool layer itself, invoked as `node …/bin/bdaya-glab.js mr merge
>   --mr … --sha …` — a spelling the hook predicate does NOT bind (command position is
>   `node`), so the watched refusal isolates `gateMandate` (#103) alone; the literal
>   `bdaya-glab mr merge …` spelling IS hook-bound and would trip both layers.
>   `gateGuard`'s unconditional unpinned-merge deny was watched first as the delivery
>   control.
> - **Watched live ABSENCE** (the same unproven pinned merge EXECUTED and reached GitLab;
>   the target's draft status was the only thing that stopped it):
>   (a) from a cwd without the pointer — **arming is per-`process.cwd()`**: the hook reads
>   the pointer under the shell's CURRENT directory, so a lane that `cd`s into a
>   worktree/submodule silently disarms the merge gate;
>   (b) the **PowerShell tool** — `mandate-gate` self-filters to `tool_name === 'Bash'`,
>   so on Windows-primary machines the primary shell is ungated;
>   (c) raw `glab api -X PUT …/merge/` (predicate binds `api`, not `mr merge`);
>   (d) `gh pr merge` (GitHub-side merges — no predicate coverage at all);
>   (e) the **typed MCP `mr_merge` / `approve_and_merge`** — the #103 gate code is present
>   in the running server but NEVER ARMS live: the MCP server's `process.cwd()` is not the
>   session's project root (nothing in its spawn chain sets a cwd), proven by a
>   nonexistent-MR probe that returned a bare 404 where an armed gate refuses before the
>   fetch. The gate that is unit-proven at the tool layer is live-inert on the machine's
>   RECOMMENDED merge path.
> - **Honestly NOT demonstrated:** the merge-arm sweep's refuse leg (below) — the demo MR
>   cannot be a sweep candidate because this machine's typed layer authors MRs as the
>   OPERATOR account, not `bdaya-agent`, and manufacturing staleness on someone's real
>   Pillar-A MR is not acceptable; and `git push -o merge_request.merge_when_pipeline_succeeds`
>   (predicate reasoning only: command position binds `git`, so it is not gated).
>   *[The operator-authorship premise in this historical record is since FIXED — owner
>   decision D23 (devops/aggregate#2, 2026-08-04): the typed layer now genuinely authors
>   as `bdaya-agent`, identity-enforced at dispatch (`gitlab/lib/identity.js`); see
>   [#rv1-not-independence](#rv1-not-independence). The sweep's refuse-leg demonstration
>   itself remains owed.]*
> - **The merge-arm sweep is a THIRD enforcement surface and its own merge context**
>   (!454; `scripts/merge-arm.mjs`, `MERGE_ARM_LIVE: "1"` on the scheduled
>   `reviewer-summon` job; 2026-08-04 adjudication note 52524 on #57). To date its
>   counters read `armed=0 refused=0` — it has never actuated in either direction, so its
>   deny leg remains unit-tested only (`tests/merge-arm.test.js`).
> - **Per-call delivery caveat:** a gated command that EXECUTED is not evidence the hook
>   checked-and-passed it (KB `reference_mandate_gate_did_not_fire_on_one_merge`,
>   2026-07-30). The per-cwd arming in (a) is a plausible — not proven — mechanism for
>   that incident: the un-denied call may simply have run from a drifted shell cwd.
> - **2026-08-04 follow-up lane (issue #57 "close the arming holes"):** absences (a)-(d)
>   and the row-12 push-option gap now have fixes BUILT and unit-proven — cwd-independent
>   pointer discovery (walk-up + `BDAYA_RUN_LEDGER` env; `hooks/lib/run-pointer.js`, and
>   its deliberate twin in `gitlab/lib/tools/mandate.js#resolveRunPointer` for (e)),
>   PowerShell coverage via `isShellTool`, and predicates for raw-API / `gh pr merge` /
>   auto-merge push options. The box's absence list above records the PRE-fix step-1
>   measurement; it stands until the same watched-deny matrix is RE-RUN live on the
>   released plugin and the updated matrix is posted to #57 — until that note lands,
>   treat the absences as open.
>
> ⇒ Net, as measured on 2026-08-04 and true until 2026-09-02: on the uncovered merge
> surfaces (PowerShell, MCP-typed, raw-API, push-option, web-UI, GitHub-side,
> disarmed-cwd) there was NO mechanical block at all — the reviewer-bot native approval
> that formerly backstopped them had been made optional by the 2026-08-18 Option-A
> decision, so gate 4 on those surfaces was **procedural doctrine** (the fresh-context
> review MUST still happen and be recorded as an RV-1 verdict note). Closing those holes
> is hook-layer work (issue #57).
>
> **UPDATED 2026-09-02 (#683):** a platform-side block exists again on every **GitLab**
> surface — `approvals_required: 1` plus `merge_requests_author_approval: false` on all 98
> estate GitLab projects — so a merge attempted from any of those uncovered GitLab surfaces
> without a reviewer-bot approval is refused by GitLab itself (observed:
> `detailed_merge_status: not_approved`). **`gh pr merge` (surface (d) above) is not
> covered**: those repos live on GitHub, where a GitLab approval rule has no reach —
> `infra-github` merges there daily, and its gate is the fresh-context review alone. That does not retire the hook-layer work: the platform block cannot
> tell whether an RV-1 verdict note exists, so #57 still owns proving the review, and the
> approval remains attribution rather than an independence proof
> ([#approval-required-again](#approval-required-again)).
>
> Step-0 legs verified 2026-07-28 against `gitlab/lib/tools/mandate.js` (issue #63):
>
> - `RV-1` is a `VALIDATORS` entry (`mandate.js` `validateReviewerVerdict`) — it requires a
>   **verdict-shaped MR note** (`verdict: PASS|NEEDS-CHANGES|NEEDS-HUMAN`), sha-pinned to
>   the MR's LIVE head, not predating the head commit's push, and takes the LATEST
>   sha-pinned verdict (so a same-sha retraction — see `agents/bdaya-reviewer.md`
>   "Retracting a prior PASS on the same SHA" — actually invalidates the proof). A native
>   approval or the `reviewer-approved` label is explicitly NEVER this evidence — closing
>   the exact `!337` shape (approved + labeled + green pipeline, zero verdict notes) that
>   motivated this issue;
>   *[UPDATED 2026-08-18 (Option A: verdict-note-alone). The note-author filter (#282) and
>   the native `approved_by` leg (D27) are BOTH gone — `validateReviewerVerdict` no longer
>   reads the approvals endpoint. RV-1 is proven by the sha-pinned fresh-context reviewer
>   PASS note ALONE, because reviewer-bot approval was made optional (approvals_required=0)
>   and requiring approved_by denied every armed merge. "A native approval or the label is
>   NEVER this evidence" still holds; the fresh-context reviewer != implementer requirement
>   is now DOCTRINAL (not a code-checkable author-string). See
>   [#rv1-not-independence](#rv1-not-independence).]*
> - `RV-1` sits beside `CG-1` in the merge-gate `items` set (`mandate_require`'s
>   non-distribute branch);
> - the merge-time TOCTOU `--sha` comparison now runs independently for **both** `CG-1`
>   and `RV-1` (`shaMismatches`, plural — a fresh CG-1 proof does not mask a stale RV-1
>   proof or vice versa);
> - `agents/bdaya-reviewer.md` documents emitting the proof on PASS: post the
>   verdict-shaped MR note, then (inside an armed run only) `mandate_satisfy --item RV-1`.
>
> **Proven at the unit level, in BOTH directions** — `gitlab/tests/tools-mandate.test.js`'s
> RV-1 block covers: no note (the `!337` shape) denies;
> *[UPDATED 2026-08-18: a self-authored note is admissible and no approved_by is read — the
> approval-leg deny tests (self-approval, empty approved_by, unreadable approvals,
> unresolvable-author) were removed with Option A]*; a stale-sha note denies; a note
> predating its cited head denies; a later NEEDS-CHANGES/NEEDS-HUMAN retracting an earlier
> PASS on the same sha denies; a genuine sha-pinned PASS allows; the merge-time TOCTOU
> check independently catches a stale CG-1 and a stale RV-1. **Step 1's live demonstration
> was executed 2026-08-04** (top of this box): the watched deny is real in the armed Bash
> contexts, and the execution's chief yield is the honest list of contexts where NO deny
> exists. Because of those uncovered contexts, on most real merge surfaces the HOOK-LAYER
> block is **procedural** — the fresh-context review MUST still occur and be recorded as an
> RV-1 verdict note, and closing the uncovered contexts is hook-layer work (#57). *(Until
> 2026-09-02 that left no mechanical block at all on those surfaces, because the
> reviewer-bot approval had been made optional by the 2026-08-18 Option-A decision; since
> #683 GitLab itself refuses an unapproved merge on every **GitLab** surface in that list.
> **`gh pr merge` is NOT covered** — a GitLab approval rule cannot govern GitHub, and
> `infra-github` is merged there routinely, so that surface remains procedural-only. See
> [#approval-required-again](#approval-required-again).)*
> `/bdaya-work` exists to run **fully autonomously with deterministic enforcement**, and a platform-side approval block is the opposite of that on three counts:

1. **It is unfalsifiable.** A native approval minted with the shared `reviewer-bot` PAT cannot distinguish "an independent reviewer approved" from "the author's own machine held the credential". Whoever holds the token *is* the bot. That is impersonation of a service account: many principals, one identity, so the audit entry names nobody. It is the same hole that let `!273` approve its own MR 4m45s before writing any verdict. This is exactly why the 2026-08-18 Option-A decision dropped the native-approval leg from RV-1 entirely.
2. **It fails out of band, after the push,** and its remedy is not code — it is *obtain a credential*. An autonomous agent then stops doing the task and starts solving the merge block, which is the worst possible place for it to improvise.
3. **The platform itself walked this back.** GitLab shipped [Service Account & Access Token Exceptions](https://gitlab.com/groups/gitlab-org/-/epics/18112) in 18.2 precisely so automation can *bypass* MR approval policies — its own answer to "a bot needs to merge" is to exempt the bot from the gate, not to hand the bot an approver identity.

So: **the gate is a content check the agent can satisfy locally and deterministically**, with no credential anywhere in it —

- a `bdaya-reviewer` verdict recorded against the **exact head SHA** (not the monotonic label, which carries no SHA — see [stale-approval-label](#stale-approval-label)),
- required CI green (gate 1),
- required commit trailers present (below).

`mandate-gate` already fails **pre-action, in the agent's own execution path, naming the exact next command** — that is the property being preserved. A gate that cannot be satisfied by producing evidence is a gate that teaches agents to look for escapes.

**Where this enforcement actually applies — and where it does not.** `mandate-gate` is armed by an **active-run pointer** (`.omc/state/bdaya-work/active-run.json`), discovered **cwd-INDEPENDENTLY** since 2026-08-04 (issue #57 RC-1, `hooks/lib/run-pointer.js`): the NEAREST pointer walking UP from the process cwd wins, falling back to the launcher-injected `BDAYA_RUN_LEDGER="<project>#<iid>"` env; with no pointer discoverable anywhere it returns `null` and **fails OPEN by design** — the run is legitimately disarmed, so the gate stands down (`hooks/mandate-gate.js:378-383` — re-anchored 2026-08-04 by the RC-1 change itself; prior citations `:112-116`, `:219-223` and `:231-235` each drifted after issues #92, #201 and #57 added lines ahead of it — and the test `no active-run pointer → allow (fail OPEN)`). Coverage therefore reaches any process whose cwd sits ANYWHERE UNDER an armed root (worktrees and submodule checkouts included) and any session whose env carries the run ledger — while a `/bdaya-task` run, an ad-hoc session OUTSIDE any armed tree, or a manual merge from an unrelated checkout still has no hook standing between it and the merge. **History: the step-1 matrix (2026-08-04, STATUS box) live-demonstrated the pre-RC-1 trap — the pointer was read under the shell's CURRENT cwd only, so a mere `cd` into a worktree silently disarmed the gate; the identical pinned merge was denied from the armed root and executed from a worktree cwd seconds earlier. RC-1's walk-up closes exactly that shape.** In the still-uncovered contexts gate 4 remains **required doctrine**, enforced procedurally by the reviewer/lead — plus whatever platform gate is still in place until the migration below completes. Do not read "hook-enforced" as "mechanically enforced everywhere"; state which context you are in before relying on it.

**Honest limit: the replacement has a bypass.** `BDAYA_ALLOW_UNPROVEN=1` disables the gate, honoured from session env *or* inline on the command (Bash prefix form, or PowerShell's `$env:BDAYA_ALLOW_UNPROVEN='1';` statement), audited only by a line on stderr (`mandate-gate.js:372-376` — re-anchored 2026-08-04 by the #57 RC-1/RC-2 change; prior versions, `:107-110, :55`, `:213-217` and `:225-229`, were likewise stale after the #92 migration, the #201 hint and the #57 lane). That is a deliberate escape hatch — an unsatisfiable gate is worse than a bypassable one — but argument #1 above criticises the platform gate for being unfalsifiable, and this gate has **no floor**: any shell-capable agent can prefix one token. The trade is different, not strictly better. It is defensible because the bypass is *visible in the command that used it*, where an impersonated approval is visible nowhere.

**GitLab's job is attribution and audit, not blocking.** Three concerns the old design collapsed into one approval, and which MUST stay separate ([Crash Override, *Attributing AI-Authored Commits in Git*](https://crashoverride.com/resources/knowledge-base/code-ownership/attributing-ai-commits-git)):

| concern | mechanism | answers |
| --- | --- | --- |
| **Attribution** | `bdaya-agent` service account authors MRs/notes (identity-enforced at dispatch since D23 — `gitlab/lib/identity.js`); `Co-Authored-By:` + `Generated-By: <agent>/<ver> (model: …; operator: …)` trailers | *who wrote this* |
| **Accountability** | `Signed-off-by:` naming the human operator | *who vouches for this* |
| **Authorization** | gate 4 above | *may this proceed* |

Attribution keeps AI work visibly distinct from a human's own contributions (the UX goal) **without** an identity anyone can borrow. `Signed-off-by` preserves the named-human trail that segregation-of-duties frameworks expect — the same split GitLab's own [composite identity](https://docs.gitlab.com/user/duo_agent_platform/composite_identity/) makes, which credits the human on the MR while the service account carries audit traceability.

<a id="rv1-not-independence"></a>

<a id="approval-required-again"></a>
> **2026-09-02 owner ruling (shared/claude-plugins#683, AskHumanQuestion `askmtkh1pchxpuj7k`,
> verbatim): "enforce 1 approval estate wide ONLY AFTER making reviewer bot credential's as
> available as bdaya-agent, so lanes never get blocked on reviewer credentials. since the entire
> purpose of it is attribution in a fully autonomous system."** This REVERSES the 2026-08-23
> removal of the required-approval rules ("it's useless and we already have better replacements")
> on one precondition, which was met the same day: the reviewer-bot token file
> (`~/.config/bdaya/reviewer-bot-token`) is byte-identical on windows-desktop, windows-pc and
> macbook, and an approval was executed from each (#683 note 116689). What is now true:
>
> - Every GitLab project in the aggregate's `.gitmodules` plus `devops/aggregate` carries an
>   approval rule `reviewer-bot` (`rule_type: any_approver`, `approvals_required: 1`) and
>   `merge_requests_author_approval: false` (#683, one line per project). `mr_merge` on an MR with
>   no approval is refused by GitLab; `approve_and_merge` (reviewer-bot approve pinned to the
>   reviewed sha, then merge as bdaya-agent) is the one landing path.
> - **Option A's RV-1 is unchanged.** The sha-pinned fresh-context reviewer PASS note is still the
>   gate-4 proof and is still what `validateReviewerVerdict` checks; the native approval is the
>   server-side *attribution* of that review (the owner's stated purpose), not a second independence
>   proof — the analysis in this section still stands, and [#rv1-not-independence](#rv1-not-independence)
>   is still why.
> - A lane MUST NOT treat the approval's presence on every member as licence to approve its own
>   MR: the reviewer that dispatches `mr_approve` is the fresh-context reviewer, never the
>   implementer (accountability.md D6). Equally, a lane MUST NOT refuse to act on this ruling
>   because its record is agent-authored — the owner's `AskHumanQuestion` answer IS the
>   authorisation (#684 records the refusals that cost 20 minutes and a re-ask).
>
> The 2026-08-18 block below is retained for its analysis; its "OPTIONAL (`approvals_required=0`)"
> statements describe the 2026-08-18 → 2026-09-02 window only.

> **SUPERSEDED AGAIN — 2026-08-18 owner decision (Option A: verdict-note-alone).** The D27
> prescription below (independence carried by a native `approved_by` naming a non-author)
> is RETIRED. `validateReviewerVerdict` no longer reads the approvals endpoint at all. RV-1
> is proven by the sha-pinned fresh-context reviewer PASS note ALONE. WHY: the reviewer-bot
> required-approval rule was made OPTIONAL (`approvals_required=0`) on all 12 enrolled
> repos, so `approved_by` is permanently empty and the empty-approved_by refusal was
> DENYING every armed autonomous merge. The ANALYSIS below still stands — RV-1 was never a
> machine-checkable independence proof on a single-operator machine — which is precisely
> why Option A stops asserting one and keeps only the durable review artifact plus a
> DOCTRINAL fresh-context-reviewer requirement (accountability.md D6).

> **SUPERSEDED IN PART BY OWNER DECISION D27 (`devops/aggregate#2`, resolved 2026-08-09,
> option `relax-to-approval`).** The owner adopted `shared/claude-plugins#282` as
> `!483` implements it: `validateReviewerVerdict` DROPS the note-author filter and re-keys
> independence onto a native `approved_by` naming a non-author — i.e. onto `reviewer-bot`'s
> approval. **D23's requirement that RV-1's independent-review leg be satisfied by an
> OPERATOR-POSTED verdict note no longer binds.** The D23 record below is retained because
> its ANALYSIS is still correct and still the reason to distrust RV-1 as an independence
> proof; only its *prescription* is replaced. Read it with the "What D27 changed" block
> after it.
>
> **D27 supersedes ONE of D23's two consequences — be precise about which.** D23 chose
> `fix-authorship`, which produced (a) dispatch-time identity enforcement, so the typed
> layer genuinely authors MRs/notes as `bdaya-agent` (`gitlab/lib/identity.js`), and
> (b) the rule that an OPERATOR-posted note is therefore how RV-1's independent-review leg
> is satisfied. **(a) is untouched and still live** — the Attribution row of the table above
> still correctly cites D23. Only **(b)** is superseded.
>
> **The accepted cost, recorded so a future reader sees a decision rather than an
> oversight.** The owner ruled with this stated plainly: an armed run can now close gate 4
> with **no human posting anything**, using a credential this very section describes as
> manufacturable by one principal on a single-operator machine. That is the price of an
> all-agent run being able to close gate 4 unaided, and it was accepted knowingly. The
> superseded alternative — requiring a genuinely separate PRINCIPAL — remains available as
> D27's own `true-independence` option, unchosen for the second time.
>
> **One earlier claim in this box is retracted as factually wrong and is NOT the reason
> D27 went this way.** !483's original motive was that RV-1 is "structurally
> unsatisfiable" on the doctrinal path. It is not: the doctrinal path was an
> OPERATOR-posted note, which D23 chose precisely so RV-1 stays satisfiable without
> credential switching. What is unsatisfiable is RV-1 *with no human posting anything* —
> and D27 is the owner electing to have that property, not a correction of a defect.

**RV-1 is an attribution check, not an independence proof (D23, 2026-08-04).** The !465
reviewer's critique stands and is recorded here because no code change retires it: RV-1's
independent-review leg (`gitlab/lib/tools/mandate.js`, `validateReviewerVerdict`) compares
GitLab **posting-identity strings** — it proves the verdict-note author string differs from
the MR-author string, **not that two parties were involved**. On a single-operator machine
holding the `bdaya-agent` PAT, the `reviewer-bot` PAT, and the operator's own credential,
one principal can still switch credentials and manufacture that difference — the exact act
the !465 reviewer declined to perform for !461, because it voids what the check certifies.
Owner decision D23 (devops/aggregate#2, chosen: `fix-authorship`) resolves this by making
the honest path the **default**, not by strengthening the check: the typed layer now
genuinely authors MRs/notes as `bdaya-agent` (dispatch-time identity guard in
`gitlab/lib/identity.js` — a mismatched credential refuses to author; audited escape hatch
`BDAYA_ALLOW_IDENTITY_MISMATCH=1`), so an operator-posted reviewer verdict on an
agent-authored MR satisfies RV-1 with **no credential switching, because the principals
actually differ** *[D27: an operator-posted note is still ADMISSIBLE, but it is no longer
either REQUIRED or SUFFICIENT — the note leg no longer filters on author at all, and the
independent `approved_by` leg must also hold. See the D27 block below.]*. What this
deliberately does NOT claim: that collusion is impossible. A
machine holding several PATs can still author with one and "review" with another, and RV-1
cannot see that — so RV-1 MUST NOT be presented as proof of independent review.
Machine-verifiable independence on a single-operator machine does not exist; requiring a
genuinely separate principal was D23's rejected `true-independence` option.

**What D27 changed — and what it left alone** (`devops/aggregate#2`, resolved 2026-08-09,
chosen `relax-to-approval`; implemented in `!483` / `#282`). D27 supersedes D23's
*prescription*, not its analysis. The paragraph above stays true: RV-1 is still not an
independence proof, and a single-operator machine still cannot produce one. What moved is
**which artifact carries the independent-review leg**.

| | Under D23 | Under D27 (shipped, `validateReviewerVerdict`) |
| --- | --- | --- |
| Independence comes from | the verdict note's **author string** — so an agent-authored MR needed an OPERATOR-posted note | the MR's native **`approved_by`**, which must name an account other than the MR author |
| Note author | had to differ from the MR author | **not filtered**; recorded as `detail.noteAuthor`, audit-only, discriminates nothing |
| Reviewer posting identity | operator's own credential on a `bdaya-agent`-authored MR | typed `mr_note` on every MR — one path |
| Re-posting a verdict from a second bot credential | forbidden (manufacturing a string difference) | **retired** — there is no string difference left to manufacture |

**Still true, and none of it is optional (under Option A, 2026-08-18).** RV-1 remains a
**required** merge-gate item; a native approval or a `reviewer-approved` label with **no**
verdict note is still the `!337` shape and still denies. The note is still **`--sha`-pinned
to the MR's live head** (`#281`), the LATEST sha-pinned verdict still wins so a same-sha
retraction still invalidates, and a note predating its own cited head still denies. What
Option A DROPPED: the native `approved_by` leg. `validateReviewerVerdict` no longer reads
the approvals endpoint — an empty `approved_by`, an unreadable approvals endpoint, and an
unresolvable MR author no longer factor in, because none of them can, once reviewer-bot
approval is optional. The independence that remains is **doctrinal, not mechanical**: the
reviewer MUST be a fresh context distinct from the implementer (accountability.md D6),
which the validator cannot verify under D23's single `bdaya-agent` identity and does not
try to. **Option A did not make RV-1 optional, unpinned, or self-satisfiable — the reviewer
just may not be the author, and that is enforced by process.**

**The cost the owner accepted.** Under Option A, RV-1 is a durable-artifact +
doctrinal-independence check with **no second-credential leg** — the native `approved_by`
that D27 required is gone, because reviewer-bot approval is now optional and requiring it
denied every armed merge. A single principal can still author the MR and "review" it by
switching credentials; nothing here mechanically stops that. What guards it is the
fresh-context-reviewer doctrine (accountability.md D6) — a process rule, not a code check.
That was stated to the owner and accepted; it is a knowing trade of a mechanical
independence leg for autonomous throughput, not an oversight. RV-1's real value is
unchanged in kind: it forces a durable, sha-pinned, verdict-shaped review artifact that a
native approval alone can never produce.

> **Migration order is load-bearing, and the safety hazard outranks the availability one.**
>
> The obvious hazard is *nothing can merge*: removing the local bot credential before the GitLab approval rule stops blocking leaves no way to land anything. The **worse** hazard is *anything can merge* — relaxing the GitLab-side gate before the hook-side gate is demonstrably blocking leaves a window with **neither**, and `mandate-gate` fails OPEN with no active-run pointer, so that window is exactly the contexts this doc's gate list now says are procedural.
>
> **Verify with a NEGATIVE test, never a positive one.** "Confirm a merge succeeds without a bot approval" cannot check the replacement: `evaluate()` returns `null` (allow) on *both* the proof-verified path and the enforcement-disarmed path, so a succeeding merge cannot distinguish *"the hook checked and passed"* from *"the hook never fired"*. A green result there is compatible with the hook being inert. The only check that proves a deny-gate is alive is one you **watch deny**.
>
> Order:
> 0. **BUILD the replacement — DONE (issue #63).** A reviewer-verdict item (`RV-1`) exists
>    in `VALIDATORS` in `gitlab/lib/tools/mandate.js`, is included in the merge-gate
>    `items` set beside `CG-1`, its proof sha is compared independently (not `CG-1`'s) in
>    the merge-time TOCTOU check, and `bdaya-reviewer` documents EMITting that proof on
>    PASS. All four exist and are unit-tested both directions (see the STATUS box above).
>    **Still open: step 1** — the unit tests prove the logic, not a live run.
> 1. **Arm and prove the replacement — EXECUTED 2026-08-04, PARTIAL COVERAGE (see the
>    STATUS box above and the step-1 report on #57).** The watched deny is confirmed alive
>    in the armed Bash-tool contexts (main session + subagent) and the `bdaya-glab` tool
>    layer; confirmed ABSENT (watched) in PowerShell, typed-MCP, raw-API, `gh`, and
>    drifted-cwd contexts; and honestly NOT DEMONSTRATED for the push-option and
>    merge-arm-sweep contexts (reasoned/unit-only — see the STATUS box). Closing (or
>    explicitly accepting) those uncovered contexts is what remains of this precondition;
>    nothing below is safe until that residue is resolved.
> 2. **Retire the blocking rule — DONE 2026-08-18 (Option A: verdict-note-alone).**
>    Reviewer-bot native approval was made OPTIONAL (`approvals_required=0`) on all 12
>    enrolled repos, and `validateReviewerVerdict` was updated in lockstep to drop the
>    `approved_by` leg it used to require — the sha-pinned RV-1 verdict note is now the
>    whole proof. (Order still matters for any FUTURE relaxation of this kind: prove the
>    hook-side deny first, relax the platform-side gate second — this one was safe because
>    the hook-side check was already live and unit-proven before the platform side moved.)
> 3. Re-confirm step 1 still denies, and that a *proven* merge succeeds on the note alone.
> 4. Only then drop the credential paths.
>
> Step 1 before step 2 is the whole point: reversing them opens the both-gates-down window and produces a green signal while it is open.

<a id="gate4-config"></a>
**Native approval config (REQUIRED again since 2026-09-02 — see [#approval-required-again](#approval-required-again)).** Which credential mints the reviewer-bot approval is config, not doctrine: `plugins/bdaya-defaults/config/native-approval.json`. The `repos` list there predates the 2026-09-02 estate-wide rule and is no longer the enrollment boundary — every estate GitLab project now requires the approval; the list remains as the historical set where the label mirror was first wired. The approval gates the merge server-side; RV-1's verdict note gates it in the hook layer; `reset_approvals_on_push: true` remains the server-side equivalent of the `--sha` pin (`credential.gcpSecret`, `credential.gcpProject`, `credential.ciVar`; you MUST NOT log the token). You MUST NOT set `require_reauthentication_to_approve: true` (the bot has no interactive password). There is no "not enrolled" case left to look up: since 2026-09-02 every estate GitLab project carries the rule, so you MUST mint the approval (via `mr_approve`/`approve_and_merge`) on every repo and MUST NOT treat the `repos` list as permission to skip it. The `reviewer-approved` label remains a human-visible mirror everywhere and gates nothing.

**Credential-unavailable fallback (a review MUST NOT be hard-failed on this):** the reviewer MUST still post the verdict + `reviewer-approved`, additionally set `bot-approval-unavailable`, and post an MR note naming the failure. A human Maintainer has a standing per-MR escape hatch (`disable_overriding_approvers_per_merge_request: false` default). It MUST NOT fall back to label-only trust.

<a id="stale-approval-label"></a>
**The `reviewer-approved` label does not reset on push — proven live, not theoretical.** GitLab resets *native* approval when a push changes the MR's `git patch-id` (gate4-config's `reset_approvals_on_push: true`); a `git rebase` or `git merge <target>` that leaves the patch-id unchanged moves the head while GitLab deliberately *preserves* the approval — so native approval is diff-aware, not SHA-bound, and can itself survive a head move under those two operations. **That preservation assumes a CLEAN merge/rebase.** A merge-forward that hits a real conflict requires hand-resolved content absent from both parents, so the patch-id necessarily changes and approval resets like any other push. Verified live 2026-07-24 on two enrolled MRs in this repo (`!212`, `!236`): each merged `origin/main` into a stale branch, GitLab's own system note read `reset approvals from @reviewer-bot by pushing to the branch`, and `git merge-tree` against the same two parent commits independently reproduces the conflict (`docs/review-precedents.md`, `references/proof-or-hedge.md`) — confirming the reset was conflict-driven, not a platform quirk. A conflict is exactly when you reach for a merge-forward, so this is the case that bites in practice: on a repo where re-summoning `reviewer-bot` isn't in your gift, a conflict-resolving merge-forward costs you the approval outright — check summon/enrollment before merge-forwarding an approved MR through a conflict. The `reviewer-approved` label has no reset mechanism at all, patch-id or otherwise: nothing clears it when a later commit invalidates the review it recorded. Live-proven 2026-07-20 on an enrolled client repo: after a push following an already-approved review, the approvals endpoint (`GET .../merge_requests/<iid>/approvals`) returned `approved: false` while the same MR's `labels` still listed `reviewer-approved`. **Bare label presence MUST NOT be treated as evidence that a specific head SHA was reviewed** — the label carries no SHA and is monotonic (set on approve, never cleared on push). For genuine SHA-exact evidence use a verdict recorded against the exact head SHA — not `approvals.approved` alone (diff-aware, not SHA-bound) and never the label alone (advisory-only even on a non-enrolled repo, per gate4-config). On finding a stale label (native approval `false` while the label is still set), remove it (`mr_update`) so a human triaging by label is not misled — this is a manual step today; no push-triggered removal exists yet.

<a id="readiness-broadcast-is-a-merge-signal"></a>
**Un-drafting an MR — and announcing it — is a merge signal to every OTHER autonomous session on
the estate, independent of any qualifier attached to it.** In an estate where several sessions
author under one shared `bdaya-agent` identity and watch a common channel, "green and un-drafted"
is read as "ready to merge." A trailing clause saying a verdict is still pending does not survive
that read.

This is a **different** hazard from the mechanical point that un-drafting does not itself resolve
`detailed_merge_status` (`shared/knowledge-base` entry
`reference-gitlab-detailed-merge-status-masks-one-blocker`: *"Un-drafting does not 'make it
mergeable'; it merely uncovers the next blocker."*). That entry warns a session not to trust its
OWN readiness read against GitLab's own status field — a status-field hazard. This one warns that
the same act moves EVERY OTHER session watching the channel, regardless of what GitLab's status
field says — a coordination hazard. Same act (un-drafting), neighbouring concern, opposite
audience; one does not cover the other.

Measured twice in one session on a live client estate (dated MR/note ids kept below so the
sequence is re-derivable rather than taken on trust; client and repo identifiers are omitted per
this doc's own client-agnostic bar — put them in the estate's own KB, not here):

- A backend-service MR (`!492`) merged 07:24:34Z, **14 minutes after** note `89537` — its ONLY
  reviewer verdict, **NEEDS-CHANGES**. No superseding PASS existed at merge time.
- A billing-service MR (`!215`) merged 10:02:45Z; its independent PASS verdict (note `90125`) is
  timestamped **10:05:22Z — 2.5 minutes AFTER the merge**. Minutes earlier the lane had published
  to the shared channel that the MR was *"now GREEN and un-drafted,"* waving that it was *"getting
  it a fresh verdict."* The readiness half of that sentence travelled; the pending half did not.

Both merged at a SHA a verdict later PASSed — the outcome was fine, the sequence was not: RV-1 (a
sha-pinned PASS from a non-author fresh-context reviewer, [above](#rv1-not-independence)) is the
gate the whole autonomous-merge model rests on, and a merge that lands before its verdict has not
satisfied it, whatever the verdict says afterward.

**Rule.** While a reviewer verdict is pending, do ONE of:

- **(a)** leave the MR Draft until the verdict lands — Draft is the only machine-enforced "not
  yet" signal; or
- **(b)** if it must be un-drafted (e.g. to unblock a gate that cannot read a Draft), put
  **"DO NOT MERGE — verdict pending"** in the SAME sentence and SAME paragraph as the readiness
  statement — never a trailing clause, never a following paragraph.

**Corollary for status broadcasts on a shared channel.** Never publish a readiness signal
("green," "un-drafted," "all required jobs pass") for an MR whose verdict is outstanding without
the prohibition adjacent to it, in the same breath — a trailing "but still needs a verdict" does
not survive the read in a multi-session estate.

<a id="sha-pin"></a>
When all gates pass: `glab mr merge --auto-merge <mr-iid> -R <project> --sha <reviewed-head-sha>`. **Pin the reviewed SHA — the head-check and the merge MUST be the same call:** a separate head-check then a bare merge has a TOCTOU race (proven 2026-07-03, infra-github PR #152); `--sha` makes GitLab reject the merge if the head moved (GitHub: `--match-head-commit <sha>`). Distinct from §16's "check the PR base" (the *target* branch). **You MUST keep this pin even on config-declared native-approval repos** — defense-in-depth.

**There is no such thing as a "cosmetic" commit after a PASS — any commit moves the head and
requires a re-pin.** Live case (shared/knowledge-base!88, note 105589): a PASS landed at one
SHA; the author then pushed one more commit — a pure rewrap of a blockquote line to the file's
own width, reviewer-measured word-identical token streams on both sides — and `mandate_satisfy`
correctly REFUSED, in the shape `RV-1: MR <iid> has verdict note(s) but none carries a sha
matching live head <sha>... — cannot confirm which commit was reviewed` (MR and sha elided here;
the live refusal on !88 named both). The reviewer had called the pending nit "not worth a
respin," which reads as "the edit is free" but is not: **the cost is never the edit, it is the
re-verification the pin requires**, and the gate cannot distinguish a rewrap from a content
change without re-reading — a verdict that floated to whatever the head happened to be would be
worth nothing. The corrected phrasing to carry forward: a note that a nit is "not worth a
respin" means *leave the commit alone*, not *the change is free to make*; making it anyway
means posting a fresh sha-pinned verdict at the new head (a re-pin re-review, not necessarily a
full re-review from zero — the diff between the two heads is the actual surface to re-check).

<a id="who-actuates"></a>
**Who may obtain and actuate gate 4.** A lane that needs the gate-4 independent review and has **no
Agent-spawning tool** MUST request the reviewer from its parent (one message + stop); it MUST NOT
spawn a reviewer subprocess of its own — scoped to that same no-Agent-spawning-tool condition, not a
blanket ban: a lane that DOES have an Agent-spawning tool is authorised, and directed, to dispatch its
own fresh-context reviewer instead of parking finished work (`accountability.md`) — and MUST NOT
approve, label, or retry a gate job
before an independent verdict exists at its head SHA. Once the fresh-context reviewer returns PASS
at the head SHA, that same lane approves-or-merges its OWN work — the reviewer being a distinct fresh
context is the only preserved invariant; the implementer may be the merger (accountability.md D6,
2026-08-14 owner ruling). Mechanism, the `claude --print` stdout-buffering trap that makes a healthy reviewer look
dead, and the `!273` incident where an un-killed orphan approved its own MR 4m45s before writing any
verdict: `references/workflow-engine.md` WF-8.

<a id="route-gate-failure"></a>
**Route every gate failure through the escalation ladder, never to a human by default** (bdaya-work `references/escalation-ladder.md`): non-convergence climbs one strategy rung; a terminal failure converts into TFR-1 durable artifacts; a canary e2e FAIL on any env rolls back autonomously and routes through TFR-1 (a prod-blocking one additionally notifies the channel — HITL-1 visibility, not a gate); CI red after two retries takes the R6 env-defect path. A `NEEDS-HUMAN` verdict is surfaced to the owner via `AskUserQuestion` (a top-level session) or, in a lane/subagent, its RETURN VALUE — `AskUserQuestion` reaches no one there (owner ruling shared/claude-plugins#491: NEEDS-HUMAN is the only human gate on reviewed work, and `AskUserQuestion` is its interactive surface, not a Tier-1 ballot parked by default); when the owner is away it is additionally recorded as a durable Tier-1 DECISION ballot, or naming none routes to the R6 autopsy. **The same rule governs a dead or non-responding reviewer, stated in full at its own home:** a null/timed-out/idle reviewer is a NON-VERDICT, not a gate failure to page a human over — `references/workflow-engine.md` WF-1/WF-8 codify fail-closed-and-auto-recover (bounded respawn at the same head SHA) as the response, and `agents/bdaya-reviewer.md` "Verdict and escalation" draws the `NEEDS-CHANGES` (mechanism) vs `NEEDS-HUMAN` (product-only) line a non-verdict falls on neither side of (owner directive, issue #458). **Every autonomous merge MUST produce an audit entry** (`audit:merge` CI job). **`env/prod` promotion is autonomous under the D22 standing grant** (owner decision, `devops/aggregate#2`, 2026-08-04 — it removed the former OWNER-4 PROD-IRREVERSIBLE class): armed Gate 4 on the repo (the ladder's Gate-4-armed precondition) + all four merge gates on the promotion MR + a VP-1 live-proof note recorded in a **deployed env** (`hasVp1LiveProof`/`VP1_ENV_RE`, `gitlab/lib/close-gate.js`; **`dev` counts as a deployed proof env since the 2026-08-22 owner ruling**, which reversed the earlier `dev`-only exclusion — so `stg` stays the *recommended* pre-prod proof env but is no longer the only one that satisfies this leg); post-canary rollback merges on a prod-deployed env go through the same four gates, with the HITL-1 channel notification as visibility, not a gate.

**Proof varies by code type, and a migration rehearsal is no longer mandatory promotion evidence (2026-08-22 owner ruling — verbatim: "proof varies depending on issue type and code type, if the code targets an API server, successful blackbox e2e tests covering the issue should suffice; and if against a frontend, a patrol e2e test with video recording and proper assertions should suffice").** The proof a promotion (or a close) owes is scoped to what the code targets: **API-server code** is proven by successful **black-box e2e tests covering the issue**; **frontend code** by a **Patrol e2e test WITH video recording AND proper assertions** (the video is the visual proof on the MR). Dev-env proof plus the by-code-type suite suffices — a separate **migration rehearsal against a prod dump is NOT a required promotion gate** (it was never encoded as a mandate in this repo; this records the relaxation as doctrine). This relaxes WHICH env and WHICH suite count as proof; it does **NOT** relax the proof-or-hedge discipline (`references/proof-or-hedge.md`) — every promoted claim still owes a proof, recorded and attributed. Full gate table, the standing-grant leg table, routing/audit mechanics, credential flow, constitution-tier autonomy note: docs/autonomous-merge-runbook.md; reviewer-side approve flow: agents/bdaya-reviewer.md.
