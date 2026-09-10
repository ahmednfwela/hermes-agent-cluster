# Git hygiene & worktrees

> Shared `bdaya-core` doctrine reference. Consumed by: `bdaya-work`, `bdaya-task`, and every
> `bdaya-defaults` agent via `Skill({ skill: "bdaya-defaults:bdaya-core" })`. Host-neutral —
> the discipline below holds on any git host (GitLab, GitHub, or a remote-less local repo).
> Normative keywords follow BCP 14 (RFC 2119, RFC 8174).

Load before ANY code work, on every invocation, before touching a submodule.

Index: worktree-ownership first-action rule · `.worktree-status` for a deliberately-held uncommitted diff · pull-first · dirty/diverged detection · orphaned-WIP disposition · isolated worktrees · worktree-sprawl audit · ask-on-ambiguity · scratch outside the isolation worktree · removing a worktree in a submodule aggregate · background-session `EnterWorktree` submodule cwd resolution · nested worktree depth breaking a submodule's own relative sibling-submodule path references · Windows CRLF staging trap · pipeline-skip token in a commit body · uninitialized submodule is not a git-command boundary · verifying a push actually landed (detached HEAD is a silent no-op) · check MR state before pushing to, or updating, a branch whose MR may already be merged.

The LEAD MUST sync git **before** touching anything. Local submodule checkouts are routinely stale and dirty (this aggregate carries dozens of dirty submodules); building on them clobbers in-flight work and renders against stale refs — a checkout tens of commits behind on a feature branch, or a `main` far behind with uncommitted WIP, is the normal case, not the exception.

## Worktree-ownership first-action rule

Before adopting any worktree, the LEAD MUST enumerate `git worktree list` first. **The LEAD MUST NOT adopt a worktree path that is already on the list** — a live session may own it, and two sessions writing the same tree is a branch-hijack hazard. It MUST claim a **suffixed path** instead (`<path>-2`, `<path>-<branch>`). The one exception is an explicit lane-resume continuation that names both the branch and the worktree to resume — then `git checkout <branch>` in that named worktree (MUST NOT use `-b`) and continue. See `references/workflow-engine.md` (WF-4 resume law) for the cross-run lane-handoff protocol.

## `.worktree-status` — marking a deliberately-held uncommitted diff

Git records WHAT changed, never WHY it is still uncommitted. A lane pausing
mid-protocol with a diff left uncommitted **on purpose**, and a second lane that later
finds that same diff, are structurally unable to tell "abandoned, safe to take over"
from "deliberately held, do not touch" — nothing in the diff itself carries that
information, so good judgment on either side is not enough to make it safe.

**Incident (2026-08-20, a production `lago-api` MR under adversarial review, !195).**
Lane A had committed RED test guards alone, leaving the production fix uncommitted so
a pending CI run would observe the guards fail before the fix landed — the proof an
adversarial review had demanded that the guarded clauses were real, not deletable dead
code. That RED pipeline went terminal well before the collision below. Lane B —
whose dispatch-time roster listed Lane A, and who correctly read the diff as Lane
A's mid-flight work, but did not know the two were on the same task — found the
uncommitted diff over 40 minutes later — after already reading the RED result itself
— and asked Lane A before acting. It did **not** wait for a reply: getting none inside
its own patience window, it moved to commit the diff under its own attribution. That
attempt found nothing to commit only because Lane A had, minutes earlier, committed
first; Lane A's "do not commit or push" reply arrived several minutes AFTER Lane B's
commit attempt, not before it. Had Lane A been a little slower, Lane B's own
transcript shows it would have committed over paused, deliberately-sequenced work
anyway — asking, then acting on a timeout, nearly caused the exact collision it was
meant to prevent, saved only by which lane happened to commit first, not by the
asking. (A collision landing instead while a CI run is still *pending* is a distinct,
real hazard this convention also guards against: `interruptible: true` cancels a
running job on the next push to its branch, which would destroy an in-flight RED
observation before it ever completes.)

**Rule — the pausing lane.** A lane holding a deliberately uncommitted diff for a
multi-step reason MUST write one `.worktree-status` file at the worktree root before
ending its turn, naming:

- **who** — the lane/agent name,
- **branch** — the branch it is on,
- **next step** — what happens next and what unblocks it (e.g. "CI pipeline <id>
  pending; commit the fix once the guards are observed RED"),
- **what must not be done** — e.g. "do not commit or push; a RED observation is
  pending."

**Rule — the finding lane.** Before touching an uncommitted diff it did not make, a
lane MUST read `.worktree-status` if present, and follow it (or reach the named lane
directly) rather than assume. **If the file is absent, the diff is unexplained — ask
before acting, and wait for an answer.** The incident above shows that asking and then
proceeding on a self-set timeout is not a safe substitute — it was one lucky commit
order away from the same collision. An absent marker is not evidence the diff is
abandoned, and an unanswered question is not permission to proceed.

**Scope and cleanup.** This is scratch, not a deliverable: the lane that wrote it
deletes it once the held step resolves and the diff is committed — not optional
cosmetic hygiene, since `.worktree-status` does not match the recognised
scratch-litter allowlist ("Scratch MUST NOT be written inside an isolation worktree"
below) and left behind would strand the tree by the same #174 mechanism as any other
unrecognised untracked path. This section specializes that one for the opposite case —
a worktree two lanes are actively **sharing**, where the risk is not stranding the tree
from auto-removal but a second lane destroying paused, live work. It does not replace
Step B item 3 below (provenance-tracing genuinely orphaned WIP of unknown age); it is
for the narrower case where the pausing lane is still live and the pause is deliberate.

## Step A — do not create isolation you already have

Before creating anything, establish whether the harness ALREADY isolated you. `git rev-parse
--git-common-dir` differing from `--git-dir` means you are inside a worktree; OMC's `EnterWorktree`
and a `worktree`-isolation subagent both put you in one on entry. **The LEAD MUST NOT create a
second worktree inside an isolated workspace** — nesting produces two trees of the same repo, so
edits land where the reviewer is not looking and the branch you push is not the branch you edited.
Detect first; create only if genuinely un-isolated.

The same rule covers the inverse: when a harness pins a working directory (a background job, a
subagent launched with an explicit cwd), the LEAD MUST work THERE rather than fighting it into a
new tree.

## Step A2 — verify a clean baseline BEFORE you change anything

Run the target's test suite (or the narrowest gate that covers it) **before** the first edit, and
record the result. Two failures this prevents, both expensive:

1. **Inheriting a red baseline and blaming your change.** If the suite was already failing, you
   will spend the session debugging someone else's defect while believing it is yours.
2. **Claiming a fix that fixed nothing.** A guard that was green before your change is not
   evidence your change did anything — the same confound that makes an un-mutated test worthless
   (`test-driven-development`, mutation-verify).

If the baseline is red, the LEAD MUST say so explicitly before proceeding, and MUST NOT fold an
unrelated pre-existing failure into its own MR without naming it.

## Step B — the numbered discipline

1. **Pull latest from every relevant remote.** `git fetch origin` each target submodule and MUST work against `origin/<default-branch>` — never a stale local HEAD.
2. **Detect dirty / diverged / wrong-branch state.** Per target: `git status --porcelain`, `git rev-list --left-right --count origin/main...HEAD`, `git branch --show-current`. The LEAD MUST surface anything dirty or behind.
3. **Resolve orphaned uncommitted WIP — the LEAD MUST NOT let it rot.** For each dirty file: trace provenance (file mtime + grep the session `.jsonl` transcripts for who/when/why), then force a *deliberate* disposition with the user — commit / `git stash push -m "<why>"` / discard. The LEAD **MUST verify currency for GitOps-managed manifests** (config-sync / ArgoCD) before committing: `git diff origin/main -- <path>` tells you if it's genuine WIP or an already-merged duplicate, and a stale commit to a synced path triggers a live reconcile that can break the cluster.
4. **Work in isolated worktrees off `origin/main`.** `git worktree add <path> -b <branch> origin/main` per submodule (OMC's `EnterWorktree` with `worktree.baseRef: fresh` is the same thing). The LEAD **MUST verify each worktree's remote is the intended repo** (`git remote get-url origin`) — a chained `cd` that doesn't persist silently makes two worktrees of the *same* repo. `gh pr merge` / `glab mr merge` merges **server-side** and does **not** advance the local `origin/<base>` ref — branching a new worktree from `origin/main` right after a same-session merge silently bases it on the pre-merge commit, no git error, missing everything that merge just landed (observed case: a whole source module absent, a CI matrix that quietly covered only 5 of 6 components, and docs that then described the missing piece as unshipped). The LEAD MUST re-run `git fetch origin` immediately before this step whenever it follows a `gh pr merge`/`glab mr merge` earlier in the same session, and SHOULD confirm with `git merge-base --is-ancestor <merge-sha> <new-branch>` (or that a file the merge added actually exists) before trusting the new worktree. The developer's dirty trees MUST stay untouched; open MRs from the worktrees.
5. **Audit and prune worktree sprawl — invisible to a plain `git status` on the main checkout.** Every invocation's step 4 (and OMC's `EnterWorktree`) leaves a worktree behind; across sessions these accumulate unbounded and are never auto-cleaned — sprawl reaches dozens of trees, many dirty, several holding genuinely unsaved source on unpushed branches. The LEAD MUST enumerate `git worktree list --porcelain` for the aggregate and every submodule. Per worktree, classify both dirty state (`git status --porcelain`) and push state — a branch with no upstream (`git rev-parse --abbrev-ref --symbolic-full-name @{u}` errors) holds work that exists nowhere else; `git cherry origin/<target> HEAD` distinguishes committed-but-unmerged (`+`) from already-merged (`-`) — most sprawl is `0/0`, purely uncommitted WIP from an interrupted turn. For dirty files, separate generated artifacts (`generated_plugin_registrant`, `generated_plugins.cmake`, `pubspec.lock`, `bin/`, `obj/`, `.dart_tool/`, `.aspire/settings`) from real source, then diff real source against the target branch (NEW / DIFFERS / SAME) — a file NEW to the target is the strongest signal of genuine unmerged work. The LEAD **MUST preserve** it: commit + push the branch (so discovery finds it by issue-number-in-branch-name, D-SPAWN-1) and open a draft MR. It **MUST discard** merged/superseded/scratch trees. It **MUST NOT touch a LOCKED worktree.** Prune with `git worktree remove --force <path>` (skip the main checkout, `.git/modules/` paths, the aggregate root, and any LOCKED entry), then `git worktree prune` — this deletes large directories, so the LEAD SHOULD run it in the background. **`bdaya-worktree-sweep` does this survey for you** — report-only by default (`--apply` to act, `--older-than-days` to set the age gate), and it classifies each tree exactly as the Stop-hook guard does. Note what makes the sweep necessary rather than redundant: a `Workflow` lane's `isolation: 'worktree'` tree is minted by the **harness**, with no `git worktree add` command and therefore no entry in the session worktree ledger the Stop-hook guard reads — so the guard structurally cannot see the very trees that leaked (#174).
6. **Ask on ambiguity.** Dirty tree, diverged branch, unclear base ref, or orphaned WIP of unknown intent → the LEAD MUST use `AskUserQuestion` **before** working. A wrong base ref is far more expensive than a question.

## Scratch MUST NOT be written inside an isolation worktree

**A lane that writes ANY scratch file into its own isolation worktree strands that worktree
permanently.** This is not a style preference; it is the mechanism behind a measured 113
worktrees / ~53 GB under one aggregate's `.claude/worktrees/` — 41 % of a 219 GB tree
(#174). The chain is short and has no self-correcting step: a scratch file makes the tree
"changed" → every auto-removal path skips a changed tree → nothing ever revisits it. The
harness's own `isolation: 'worktree'` contract ("auto-removed if unchanged") is defeated by
the lane's first `.scratch/` write, and the lane never learns, because a leak that fires no
signal grows silently (the same lesson as #164).

The audit of those 113 trees found the overwhelming majority dirty **only** with
lane-authored litter — `.scratch/`, `tmp/`, `_lane/`, `sc-work/`, an `mr<iid>-note.md` at
the root. One held 2.5 GB of `.scratch/` alone.

**Rule.** Every lane MUST write intermediate output — MR-note drafts, review scratch,
downloaded artifacts, analysis dumps, cloned baselines — **outside** the worktree:

- Prefer the job tmp dir: `"$CLAUDE_JOB_DIR/tmp"`.
- `CLAUDE_JOB_DIR` is supplied by the harness and MAY be absent (it is not set by anything
  in this repo — verify before relying on it, do not assume). Fall back to an OS temp dir
  the lane creates itself: `mktemp -d` / `$env:TEMP\<lane-id>`.
- A file that IS a deliverable belongs in the worktree **and gets committed** — the
  distinction is "does this ship", not "is this a .md".

**Cleanup is the backstop, not the fix.** `orphaned-resources-guard.js` now classifies a
tree dirty only with recognised litter as removable (`hooks/lib/worktree-cleanup.js`), but
that allowlist is deliberately narrow and closed: an untracked path it does not recognise
protects the tree, because a false "disposable" destroys work while a false "protected"
only costs a nudge. Do not treat the classifier as permission to litter — a novel scratch
name it has never seen strands the tree exactly as before.

## Removing a worktree in a submodule aggregate

`git worktree remove` **hard-refuses** a submodule-bearing tree even when it is spotless:

```text
fatal: working trees containing submodules cannot be moved or removed
```

That is a refusal about the tree's SHAPE, not its content, so no amount of cleaning
satisfies it — and in this aggregate it fires on essentially every tree (50 of one sweep's
85 removals hit it). For a tree already **verified clean**, the removal is filesystem-first:

```bash
git -C <main-repo> worktree remove <path>   # try the ordinary path first, never --force
rm -rf <path> && git -C <main-repo> worktree prune   # only after verifying clean
```

`prune` alone leaves the directory; `rm -rf` alone leaves a dangling registration that
`git worktree list` keeps reporting. Both, in that order. The LEAD MUST re-verify
cleanliness immediately before the `rm -rf` — not from an earlier snapshot; a lane may have
written to the tree in between.

## Background-session `EnterWorktree` — submodule cwd resolution

Item 4 above ("`git worktree add <path> -b <branch> origin/main` per submodule; OMC's
`EnterWorktree` with `worktree.baseRef: fresh` is the same thing") holds for the
**aggregate** repo. It does NOT hold for a **submodule**: `EnterWorktree`'s name mode
(the create path) only ever targets the aggregate — it cannot create a submodule
worktree. A background session that must edit submodule `S` MUST create the worktree
with git directly, then adopt it via `EnterWorktree`'s **path mode**.

Path mode resolves "does this path belong to me" from the **current shell cwd's** repo,
not from the path argument's location on disk. Proven live in session
`ab12fd10-bf1c-4aba-b2cf-4b590f45b28a` (2026-07-21), all three against the SAME
submodule-owned worktree path under the aggregate's `.claude/worktrees/`:

1. cwd = the **aggregate root** → `not a linked worktree of <aggregate>` (the worktree is
   registered to submodule `S`, not the aggregate, even though the path sits inside the
   aggregate's directory tree).
2. cwd = a **different submodule** → `not a registered worktree of <that submodule>`.
3. cwd = **`S` itself** → succeeds.

Until step 3 succeeds, the bg-session **bgIsolation guard rejects every `Edit`/`Write`**
call — `"This background session hasn't isolated its changes yet"` — even for files
inside a correctly-created submodule worktree already sitting under `.claude/worktrees/`.

**Working recipe** (bg session, submodule `S`, branch `B`):

```bash
git -C <aggregate>/<S> fetch origin B
git -C <aggregate>/<S> worktree add --track -b B <aggregate>/.claude/worktrees/<name> origin/B
cd <aggregate>/<S>
```

then `EnterWorktree({ path: "<aggregate>/.claude/worktrees/<name>" })`.

**Automated preflight (#48).** `hooks/enterworktree-submodule-preflight.js`
(`PreToolUse(EnterWorktree)`) detects a create-mode call (`name` mode, or a bare call
with neither `name` nor `path`) whose cwd resolves — via `git rev-parse
--git-common-dir` — under a `.git/modules/` segment, and injects an advisory pointing
at this recipe before the harness's confusing error even fires. It is silent for path
mode (already the working route above) and for any non-submodule cwd; it never denies.

## Nested worktree depth breaks a submodule's OWN relative-path references to a sibling submodule

The nested `<submodule>/.claude/worktrees/<name>` location (used above) sits **2 extra
path-depth segments deeper** than the submodule's own root. That is invisible for ordinary
work, but a submodule whose build config reaches a **sibling submodule** via a relative path
(proto references, `gen-protos`/`buf` config, an MSBuild `<Protobuf Include="../../../grpc/…">`)
bakes in a depth that assumes the submodule root — and the nested worktree's extra 2 levels make
that same relative path resolve 2 levels short, silently, at build time rather than at
worktree-creation time.

**Measured 2026-09-02** on a client backend submodule whose `.csproj` referenced a sibling
`grpc/` submodule via `<Protobuf Include="../../../grpc/…">` (3 levels up from a `src/<Service>/`
directory 2 levels below the submodule root): that relative path correctly resolved to the
sibling submodule from the backend submodule's main checkout, but from a worktree at
`<submodule-root>/.claude/worktrees/<name>/` the identical `"../../../grpc"` instead resolved
2 levels short — into a path under `.claude/worktrees/` that does not exist — and the build
failed with:

```text
Could not make proto path relative : error : ../../../grpc/common.proto: No such file or directory
```

This is a **different** failure from a sibling submodule simply being uninitialized (a
separately-tracked, closed bug where CI never checked out the sibling `grpc/` submodule at
all). Here the sibling WAS checked out; only the nested worktree's *depth* was wrong. The
shape is not specific to one client: **any** submodule whose build reaches a sibling submodule
via a relative path is exposed the moment someone follows the standard nested convention
inside it — more than one client backend's own CLAUDE.md documents this same relative
sibling-submodule proto-reference shape. One point-fix already exists for a different tool
hitting the same underlying cause: a black-box test script gained an env-var override for its
sibling `grpc/` root specifically because it "does not sit at the usual depth" inside an
isolated worktree — a one-script fix, not a documented general rule, so any other
relative-path build reference in this aggregate remained exposed until now.

**Before nesting a `.claude/worktrees/<name>` worktree inside a submodule**, check whether that
submodule's build references a sibling submodule by relative path — grep hint: `.csproj` /
`gen-protos` / `buf` configs for `"../` or a sibling submodule's directory name. If it does,
create the worktree as a **sibling of the submodule root** instead — same depth as the
submodule itself, so every `"../../../"` in its build resolves exactly as it does from the
main checkout:

```bash
git -C <submodule-root> worktree add ../.wt-<name> -b <branch> origin/main
# e.g.: git -C clients/<client>/<service> worktree add ../.wt-<name> -b <branch> origin/main
#   -> clients/<client>/.wt-<name>/   (sibling of <service>/, NOT nested inside it)
```

This is the one place in this file where the sibling-of-submodule shape is the RIGHT choice over
the nested `.claude/worktrees/<name>` default used everywhere else above — depth-sensitivity in
the submodule's own build is what flips the recommendation here, and only here.

## Windows CRLF-committed-file staging trap (`core.autocrlf=true`)

On Windows with the Bdaya-default `core.autocrlf=true`, a programmatic read+write edit of a file whose **committed blob is CRLF** can silently rewrite it to LF. A tiny logical change then diffs as the WHOLE file (`git diff` reports every line touched) and resets `git blame`. The symptom is real and was observed live; the mechanism below is the measured one.

**The EDIT flips it — the staging filter does not.** `core.autocrlf=true` is defined as "the same as setting the `text` attribute to `auto` on all files" (`git config` docs), and `text=auto` converts on check-in only "if it is text **and the file was not already in Git with CRLF endings** … Otherwise, no conversion is done" (`gitattributes` docs). So `git add` of a still-CRLF worktree copy stages CRLF **unchanged** — measured. What flips the file is one step earlier: a read+write edit that emits LF endings makes the worktree copy LF, and `git add` then faithfully records those LF bytes. The clean filter is live and does convert wherever that exemption does not apply — a path new to the index, a path whose indexed blob is LF, or one carrying an explicit `text` attribute (which has no such exemption).

⚠️ **That exemption is keyed on the INDEXED blob, not on `HEAD`** — load-bearing for the fix below, because staging the flip is exactly what removes your protection.

**Why it is easy to miss.** Not because any command hides it. The flip is invisible in every ordinary rendering: the file itself renders identically, and in the whole-file diff each removed line is byte-identical to its added counterpart but for a CR that displays as nothing — so a 100%-of-lines diff reads as a spurious reformat rather than an EOL change.

**Detection heuristic.** If `git diff --numstat` shows a change touching ~100% of a file's lines when you only edited a few, suspect an EOL flip — the LEAD MUST NOT commit it before diagnosing.

**Diagnose the TRUE committed EOL** — read the committed blob rather than the working-tree rendering. `git show <ref>:<file>` and `git cat-file blob` both emit the stored bytes **unconverted**; neither applies smudge/clean filtering nor EOL conversion (measured against firing controls, for both a `filter.*.smudge` driver and the `text`/`eol` attribute), so either is valid:

```bash
MSYS_NO_PATHCONV=1 git show <ref>:<file> | tr -cd '\r' | wc -c
```

Compare the target ref's CR count against your working tree's. A drop to 0 confirms the flip.

⚠️ **Keep the `MSYS_NO_PATHCONV=1` prefix on Windows git-bash.** It guards the `<ref>:<path>` **argument**, so `git show`, `git cat-file` and `git rev-parse` are equally exposed — switching command does not avoid it. MSYS rewrites such an argument (slashes → backslashes, `:` → `;`) for several measured shapes: a slashed ref with a dotfile path (`origin/env/dev:.gitlab-ci.yml` → `origin\env\dev;.gitlab-ci.yml`) or with a leading-slash path (`origin/main:/README.md`), while `HEAD:.gitignore` and `origin/main:scripts/x.mjs` pass through intact. The trigger is shape-dependent, so prefix unconditionally rather than predicting it. It matters more here than elsewhere: with stderr discarded, a mangled command contributes nothing to the pipe and `tr -cd '\r' | wc -c` prints **0** — which this section tells you means "flip confirmed". A silent false positive, not a visible error. See KB `internal/reference_msys_pathconv_git_show_ref_path.md`.

**Fix — restore the committed line endings in the WORKING TREE first, then stage with the clean filter off.** Both halves are required, and each fixes what the other cannot:

```bash
sed -i 's/$/\r/' <file>                 # LF-flipped worktree copy back to CRLF (only when it is pure LF)
git -c core.autocrlf=false add <file>
git diff --cached --numstat             # MUST now show only the lines you meant to change
```

- **The worktree restore is what actually undoes the flip.** No staging flag can do it alone, because `git add` records the bytes that are in the worktree and after an LF-writing edit those bytes are LF — `git -c core.autocrlf=false add` on its own leaves the whole-file diff fully intact (measured).
- **The `-c core.autocrlf=false` is what makes the restore survive staging**, and it is needed because the exemption is keyed on the INDEX. If you have **already staged the flip** — a likely state, since `git diff --cached --numstat` above is itself a staged check — the indexed blob is now LF, the committed-with-CRLF exemption no longer applies, and a plain `git add` re-converts your restored CRLF straight back to LF: measured `numstat 5 5` where the same sequence with the flag reads `1 1`. Disabling the filter is correct in **both** index states, so use it unconditionally rather than reasoning about which one you are in. (`git restore --staged <file>` before a plain `git add` also works, by putting CRLF back in the index so the exemption applies again.)

If the committed blob is already LF, a plain `git add` is correct and yields the minimal diff — the right handling differs **per file**, by that file's own committed EOL, so check it first rather than assume. A deliberate repo-wide LF normalization (plus a `.gitattributes` `text eol=lf` rule) is a separate change and MUST NOT be bundled into a functional/logic commit — it makes the functional diff unreviewable.

## Pipeline-skip token quoted in a commit BODY

GitLab scans the **whole** commit message — body included, not just the subject — for a bracketed `skip ci` / `ci skip` token, and skips that commit's pipelines when it finds one. The trap is that it fires on a *quoted, explanatory* mention as readily as on a deliberate request, so the exposed author is the one **documenting** a release flow. Releases in this repo carry the token by design (`.releaserc.json:23`, the `@semantic-release/git` message), so any commit body explaining that flow — e.g. why a post-release CI job is wrong because it clones the commit *before* the bracketed-skip version bump — silently skips its own pipeline.

The failure is quiet by construction: **both** the branch-push and the `merge_request_event` pipeline come back `skipped`, so the MR surfaces **no CI verdict at all** rather than a red one. That reads as "not started yet", not "did not run". Live-hit on `shared/claude-plugins` MR !207 — commit `fce9b1b` quoted the token inside a prose paragraph; pipelines 16146 (`push`) and 16147 (`merge_request_event`) both returned `skipped`, and an `agent-gate` FAILURE stayed invisible until the amended commit `f3e7d43` produced real pipelines 16148/16149. This is the cause behind the constitution's "a skipped or absent job is not a pass" bar (`senior-defaults.md`, Git & MRs) — that rule catches the symptom at merge time; this one stops it being created.

**Detect before pushing:**

```bash
git log -1 --format=%B | grep -iE '\[(skip|ci)[ -](ci|skip)\]'
```

**Confirm after pushing** — the new pipeline MUST be `pending`/`running`. A `skipped` status is the signature, and the LEAD MUST NOT read it as "queued".

**Avoid.** When you mean to *describe* the token rather than invoke it, write it unbracketed (`skip-ci`). In-file content, code comments, and MR descriptions are NOT scanned and MAY carry the literal bracketed form safely; this section still spells it unbracketed throughout, so a line lifted from here into a commit message cannot arm the skip.

**Recover.** `git commit --amend` + `git push --force-with-lease`, but only on a branch nobody else has based work on (the merge-only rule still binds a shared branch); otherwise land an additive commit to trigger a fresh pipeline.

## Uninitialized submodule is not a git-command boundary

`cd` into a submodule path that has **not** yet been `git submodule update --init`'d succeeds silently — the directory is merely empty, and `cd` doesn't check. Every git command run from there (`status`, `branch --show-current`, `fetch`, even `checkout -b … origin/main`) then walks UP to the nearest `.git` it can find, which is the OUTER (aggregate) repo, and operates on THAT instead — no error, no warning. `git status` reports the outer repo's clean/dirty state; a `checkout -b` creates and switches the OUTER worktree to a new branch, silently abandoning whatever branch it was on, which may carry another lane's in-flight, already-pushed work. **Verify you are actually inside the submodule before running git there** — `git rev-parse --show-toplevel`, or just `ls -la` (an initialized submodule has a `.git` file/dir; an uninitialized one is empty) — a successful `cd` proves nothing.

**Recovery, if it already happened:** the outer worktree's original branch/commit survives in its own `git reflog` — a plain `git checkout <original-branch>` restores it, and nothing is lost as long as the accidental branch/commits didn't overwrite the original ref.

**Residue check, even after a correct fix — an in-submodule checkout dirties the outer gitlink.** A plain `git submodule update --init` lands the submodule on exactly the commit the outer repo's index records, so the outer `git status` stays clean — but it lands there **detached**, and any later checkout inside the now-initialized submodule (onto a branch to do the work, or `git submodule update --remote`, which tracks the `.gitmodules` branch tip rather than the recorded pin) moves its HEAD away from that commit, which shows up as ` M <submodule-path>` under `git status --short` — a one-line gitlink diff, easy to miss among file-content changes. Under `--short` this marker IS specific to the gitlink: git uses a lowercase ` m` for modified tracked content inside the submodule and ` ?` for an untracked file there, so the three don't collapse — that distinction is `--short`-only, though, since `--porcelain` (v1) reports all three of those states as a bare ` M`, so don't reach for `--porcelain` for this check. `git submodule status`'s leading `+` is the cross-check for a genuine HEAD move. A later `git add -A` in the outer repo would stage the gitlink change, silently re-pinning the aggregate to whatever arbitrary — possibly unmerged — commit the submodule happens to be sitting on, not an intended submodule bump. After any submodule init or in-submodule checkout inside a worktree you don't own, check `git status --short -- <submodule-path>` and, if dirty, `git submodule update -- <submodule-path>` to restore the recorded pin before finishing — this aborts rather than clobbers if the submodule has uncommitted local changes the checkout would overwrite (`error: Your local changes ... would be overwritten by checkout ... Aborting`), so it's safe to run even over genuine in-progress work; commit or stash that work first if it fires. Same "you are not where you think you are" failure mode as step 4's chained-`cd` trap above (a `cd` that doesn't persist silently makes two worktrees of the same repo) — a submodule boundary instead of a worktree one.

## A pushed branch must be verified against the SERVER, never against local HEAD

`git push origin <branch>` issued from a **detached HEAD** succeeds and lands NOTHING — HEAD isn't on `<branch>` at all, so the push just re-sets the ref to the value it already had. The exit code is 0 because the refspec named did resolve to the value given; that says nothing about whether *you* just put it there.

**The push output is a real tell, and it is free — but only in one of the two shapes.** Unquoted, the no-op prints `Everything up-to-date` instead of the `To <url>` + `<old>..<new>  branch -> branch` a real push emits: *if your push said "Everything up-to-date" when you expected to land a commit, you did not land it.* Note `-q` **suppresses exactly that line**, which is why the incident below went unnoticed. So do not run the quiet form when you care whether something landed.

**But output inspection is not sufficient, and this is the case that decides it.** When the branch is itself ahead of the remote, the push lands *the branch's* commits and prints a textbook success while your detached work stays behind:

```text
# measured, git 2.53.0.windows.2, throwaway bare remote
main    = b5a0f2b   (branch, 1 commit unpushed)
my work = 8bfeb6a   (detached, on top of it)

$ git push origin main
To .../remote.git
   bc279c4..b5a0f2b  main -> main        # indistinguishable from landing YOUR work
$ git ls-remote origin refs/heads/main
b5a0f2b                                   # the BRANCH's commit. 8bfeb6a did not land.
```

Exit 0, the ref genuinely advanced, output identical to success. **That is why the server check below is necessary rather than merely tidier.**

The two checks reached for first to "confirm" the push both come back a confidently wrong yes, because both read the **local commit**, not the branch:

```bash
git rev-parse HEAD      # the commit you just made (detached), not the ref you pushed
git show --stat HEAD    # your files, present and correct, in a commit no remote has
```

Comparing against `origin/<branch>` locally is not a fix either, and can fail the same way for a different reason: under a single-branch fetch refspec (`+refs/heads/main:refs/remotes/origin/main` — common in this aggregate's submodule clones), `refs/remotes/origin/<other-branch>` can never update, not even via an explicit `git fetch origin <branch>` (that writes only `FETCH_HEAD`). So "fetch, then diff against `origin/<branch>`" is right in the common case and silently frozen in the single-branch-refspec case.

**The sound check confirms you're on THAT branch, then asks the remote directly:**

```bash
[ "$(git symbolic-ref --short -q HEAD)" = "<branch>" ] || echo "not on <branch> — a push of it from here is a no-op"
git ls-remote --exit-code origin refs/heads/<branch>   # compare the sha to the one you intended to land
```

Compare the *name*, not merely its existence: bare `git symbolic-ref -q HEAD` proves you are on **a** branch, and pushing `Y` while sitting on `X` is the same no-op class and passes it. (`-q` matters on line 1 for a second reason — it makes the detached case exit 1 silently instead of printing `fatal: ref HEAD is not a symbolic ref`.) The `ls-remote` SHA comparison is what actually decides it; line 1 just names the failure before you read a SHA.

**Recovery, if it already happened:** `git push origin HEAD:<branch>` lands the detached commit onto its branch directly. If the branch ref is simply behind, `git branch -f <branch> <sha>` + checkout + a normal push works too — assert `git merge-base --is-ancestor <branch> <sha>` first so it is a fast-forward and no force-push is needed.

`isPushedOnRemoteBranch()` in `hooks/orphaned-resources-guard.js` (~line 303) is the closest existing implementation, but **it survives the detached case by failing closed, not by asking the server**: `currentBranch()` maps `HEAD` to `null` and the function returns `false` before reaching `ls-remote`, whose call is gated behind `hasNarrowFetchRefspec()` for the frozen-refspec half. Different motivation, same conclusion — and that file's header already documents `hasUnpushed()` returning `null` on detached HEAD, so the hook side knows. This section is the missing agent-facing half.

Root cause observed live: a `git worktree add -q --detach <path> <ref>` call in which `-q` was consumed as the PATH argument, so the net effect was a detaching checkout of the CURRENT worktree at the branch tip rather than creating the new one. The next commit landed on no branch; the following `git push -q origin <branch>` exited 0 silently, and a "pushed" report went out — caught only when an unrelated MR-state read showed the MR head still at the previous commit.

The point generalizes past git: an exit code of 0 from `git push` means "the refspec you named is now at the value you gave it" — trivially already true when you were never on that branch to begin with. A successful no-op and a successful push are indistinguishable from the exit code alone; verify the SERVER state against the branch you intended, not the command's own return status.

## Check MR state before pushing to, or updating, a branch whose MR may already be merged

On a long-running feature branch, a `git push` or `glab mr update/note <iid>` issued **after** the MR already merged mid-session lands somewhere unexpected, because GitLab can auto-delete the source branch on merge — a per-project setting (`remove_source_branch_after_merge`), surfaced on the MR object as `force_remove_source_branch`. This is project-level, not an org-wide default: don't assume it from one repo, check the MR object (`glab mr view <iid>` / `mr_get`) on the project you're actually in. Observed live (session `ced90efa`): a follow-on `git push` to a branch whose MR had merged and been auto-deleted **silently RE-CREATED the deleted branch** (`* [new branch]` in the output) instead of erroring; the *first* push attempt actually failed with a `Please make sure you have the correct access rights` error — git's generic auth/repository-level failure message, not a per-branch signal, so its exact cause here was never isolated — and a blind retry succeeded and masked whatever it was. `glab mr update <iid> --description …` then **overwrote the description of the now-MERGED MR** with prose describing a brand-new round of work — a destructive write to a closed record, only caught and manually reverted after the fact. The new commit needed its own MR, discovered only by running `glab mr view <iid>` **after** the damage was already done, not before.

**This gap is adjacent to, not covered by, `orphaned-resources-guard.js`.** That hook's `glabMrStatus()` (issue #21) correctly classifies a merged-and-branch-deleted state as `clean` — but only at **Stop time**, after the turn's commands already ran. It never fires mid-turn, so it cannot stop the push or the update from happening in the first place; it can only report, afterward, that the resulting mess is "clean" (or isn't).

**Rule.** Before a `git push` to an existing MR's branch, or before ANY `glab mr update <iid>` / `glab mr note <iid>` call, run `glab mr view <iid>` (or `glab mr list --source-branch <branch> --merged -F json`, the same two-query shape `glabMrStatus()` uses) and read its state:

- **`state: merged`** → do NOT push to that branch expecting to reopen the MR, and do NOT run `mr update`/`mr note` against that `<iid>` — it is a closed record. New commits on top of merged work need a **new** branch and a **new** MR.
- **A `git push` that reports `* [new branch]` on a branch you believed already existed and had been pushed before** is itself a red flag, not a routine success — the same discipline as the section above (read the push OUTPUT SHAPE, don't just check the exit code): a genuinely continuing push reports `<old>..<new>  branch -> branch`, never `[new branch]`. Seeing `[new branch]` here means the branch you expected was gone, and something upstream (most likely: its MR already merged) deleted it. Stop and check MR state before treating the push as a normal continuation.
- A first push attempt that fails with an access-rights-shaped error, on a branch you have pushed to earlier in the same session, MUST NOT be retried blindly — that error is exactly as likely to mean "this ref no longer exists the way you think it does" as "transient auth glitch"; check server state (`glab mr view` / `git ls-remote`) before retrying.
