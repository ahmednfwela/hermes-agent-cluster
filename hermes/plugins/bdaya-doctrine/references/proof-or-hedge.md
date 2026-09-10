# Proof-or-hedge — deep law {#proof-or-hedge}

> Shared `bdaya-core` doctrine reference. The `[PROOF]`/`[UNVERIFIED]` bar itself lives
> in the always-on constitution (`senior-defaults.md #proof-bar`); this file carries the
> clean-room, job-trace, and prod-write subsections that trigger on their own failure
> signatures. Normative keywords follow BCP 14 (RFC 2119, RFC 8174).

### A local clean room is not automatically an oracle

An "isolated repro" (fresh cache, clean clone, empty state) isolates only the ONE variable you deliberately reset — it still inherits every config the tool reads from the environment (package-manager source mapping, credential/context files, resolver config). If the real defect lives in that inherited config, not in cache staleness, the clean room reproduces the failure AND confirms the wrong root cause with high confidence.

- **Warm cache and clean-cache-with-shared-config are opposite failure modes, same remedy.** Warm cache masks a real failure as a false PASS; clean cache plus buggy config masks a config defect as a false "doesn't exist / unfixable upstream" verdict. Trust the REAL pipeline (CI/prod, built from empty) over any local repro — a clean room is necessary but not sufficient as the oracle (§1).
- **Before concluding "X doesn't exist," check where a WORKING system resolved X from** — artifact provenance, a peer consumer's resolved config, or the error's own "sources not considered"-style diagnostic, which usually names exactly what got excluded.

(Case: three independent isolated-cache `dotnet restore` repros each concluded a live, published NuGet package "doesn't exist" — every clean cache still inherited a `nuget.config` `packageSourceMapping` that routed the package's pattern away from nuget.org. The real CI build exposed the one-line mapping fix.)

### A job-trace grep is not automatically an oracle

A GitLab CI job trace is not a clean tool-output stream — any job that dumps its
environment (Aspire DCP debug diagnostics, an `env`/`printenv` step, verbose
debug-logging) embeds `CI_MERGE_REQUEST_DESCRIPTION` into the trace, because GitLab
exposes the MR body through that variable. A verification gate of the form "the trace
must NOT contain `<failure string>`" then self-matches: a well-written root-cause MR
naturally quotes the failure signature as evidence, so the gate's own negative-assertion
target reappears inside the trace it is reading, and the gate reports the failure as
still present when it is not.

- **Anchor on the tool-emitted region or a structured artifact**, not a bare trace grep —
  a JUnit XML result, the presence/absence of a named artifact (e.g. `*hangdump.dmp`), or
  an artifact byte-count. If a trace grep is unavoidable, exclude the env-dump region
  explicitly before matching.
- **A bare `grep -c` over a job trace is not a sound oracle** when the trace can contain
  the MR's own text — prefer artifact-level evidence for any acceptance gate defined in
  the MR description itself.

(Case: verified live 2026-07-20 on a client `.NET` backend CI job — the target failure
string appeared 3 times in the trace; offset analysis showed only 1 occurrence was
emitted by the test runner, while the other 2 were the MR description echoed back by an
env dump, surrounded by the MR body's own markdown. A gate written in the MR description
was falsified by the MR description.)

### A vanished symptom is a lead, not a verdict

Re-verifying a reported live defect and finding the symptom gone is not the same as confirming the defect is fixed. The symptom can clear through a path that has nothing to do with any deployed fix — a fallback branch elsewhere in the stack, a hand remediation interacting with a normal reconcile loop, or retry convergence — while the buggy code path is untouched and still latent for the next actor who lacks the same accidental workaround.

- **Before closing or descoping, re-derive WHY the symptom cleared** — name the exact code path and confirm it by reading it, not by re-observing the absence of the symptom a second time.
- **Confirm the defect's mechanism is either gone from the deployed code or still latent**, at its file:line. A vanished symptom with an unchanged image/commit at that path means the defect survives for the next actor who doesn't have the same accidental workaround.
- **Distinguish three outcomes explicitly**: fixed (the buggy branch is gone or corrected in the deployed code), masked (an unrelated fallback or data change routes around the bug without touching it), or fixed-by-unrelated-change (a different, unrelated deploy incidentally corrected it). Record which one it was — a masked defect still needs the fix, and the masking mechanism belongs in the record so the next verifier doesn't close it as no-repro.

(Case: a `<Client>BillingTapProvider` CR was reported permanently `Synced=False/UpdateFailed`; a re-verify found it `Synced=True/InSync` — not because any fix deployed (the controller image was unchanged), but because the shared `lago-api` billing platform's `PaymentProviders::FindService` falls back from an id-miss to `scope.where(code:)` (`lago-api app/services/payment_providers/find_service.rb:34`): once a hand-created remediation row with the matching code existed, the still-buggy tenant-bound update silently adopted it. Any CR without that remediation row still wedges forever on the unfixed code.)

### A uniform 100%-failure audit result is an instrument-error signature, not a finding

When a dry-run/audit scanner reports **every** scanned item deficient — especially when
that contradicts the most recently recorded state of the same system (a completed
backfill's own idempotency pass, a prior clean audit note) — treat the scanner itself as
the first suspect, not the system it scanned. A 100% failure rate across a heterogeneous
population is a far stronger signal of a broken detector than of a uniformly broken
system.

- **Fetch ONE raw response from the underlying API/data source before acting on the
  scan**, and confirm the scanner's detection field actually exists in it, under the
  expected name and shape. Most languages silently read an absent or misnamed field as
  empty/falsy, so a parse miss reads exactly like a genuine uniform deficiency — the two
  are indistinguishable from the scanner's own output alone.
- **Cross-check the verdict against the most recent recorded state** (issue notes,
  completion records, a prior pass's own logged result) and treat a contradiction as
  evidence of instrument error FIRST, not of drift — drift that flips 100% of a
  population in a single pass, with no intervening change recorded, is the less likely
  explanation.
- **A deficiency heuristic that flags any grant/identity missing part of an "expected"
  full set will also false-alarm on deliberately-scoped identities** — integration
  machine users, least-privilege test personas, or any identity intentionally granted
  less than the target population's baseline. Scope the heuristic to the actual target
  population, or label known deliberately-scoped identities as expected exceptions,
  before trusting a 100%-style verdict from it.

(Case: verified live 2026-07-28 — a Zitadel grants-audit script read one field name
where the live API actually returned a differently-named field. The scanner reported
every audited org across two environments as deficient, contradicting a previously
recorded healthy state for the same population. Probing a single raw API response
surfaced the field-name mismatch and confirmed the population was, in fact, fully
healthy. Acting on the scan unchecked would have triggered a needless mass remediation
and wrongly blocked an unrelated deploy.)

### The installed plugin is not automatically the code you merged — cite the version AND the surface

"Merged" is not "in effect". The installed plugin cache lags `main`, so a hook or CLI fix cannot be verified from the shipped artifact on the day it lands — including by the session that shipped it. Worse, once an upgrade *does* land, **one session can hold two different "deployed" answers at the same time**, and which one you get depends on the surface you probe rather than on what is installed:

| surface | when it loaded its code |
|---|---|
| the CLI (`bdaya-glab`, any `node <script>`) | **per invocation** — always current on disk |
| a long-lived MCP server process | **once, at session start** — unproven after an upgrade until the server restarts |

- **Any live claim about hook or CLI behaviour MUST name the plugin version that produced it, and the surface that measured it** — the way a pipeline proof names a sha. "env: deployed plugin 2.41.1" is a statement about the machine, not about the process that answered; if an MCP tool produced the reading, the honest env is "MCP server process started under \<version\>".
- **`claude -p "<probe>" --plugin-dir <candidate>` is the sanctioned per-MR verification path** for a hook MR: a real headless session loading the plugin under test, with a delivery-control probe run first so a silent no-op is distinguishable from a pass. Promoted from one lane's improvisation (#57 note 52873, which hit a four-version gap) into doctrine.
- **The plugin-estate freshness check is ambient advice, never proof.** It reports installed-vs-latest; it does not establish which code answered a specific probe. A freshness signal that is green tells you the cache is current, not that the process holding your MCP connection reloaded.
- **The failure is symmetric, and the two directions differ in cost.** A stale server reporting the OLD behaviour is a false RED — it costs wasted investigation, and risks "correcting" an issue's text to describe a gap that no longer exists. A fix **reverted** on disk still looks present to a session whose server predates the revert: that is a false GREEN, and it ships. Treat any behaviour proof taken through a long-lived server after an upgrade as unproven until restarted.
- This is a distinct failure from an instrument that does not run (see `#273`): here the instrument runs correctly, and can even produce the other verdict — it is simply reading **a different artifact than the one the assertion is about**. No assertion about the *result* can detect it; only recording the artifact and version actually loaded can.
- **The same discipline applies to a non-reproducing test failure: name the suspected perturber, or you have not reported anything checkable.** "Unrelated flake" is an assertion nobody can falsify; "this run spawned ~400 child processes over 58s, and the failing neighbour is spawn-bounded" is a mechanism the next reader can test, confirm, or refute. Record it as advisory with its suspected cause — a flake dismissed without a named mechanism is indistinguishable from a real regression that happened not to repeat.
- **An mtime is not a modification.** A file rewritten with identical bytes (a `git restore`, a checkout, a formatter no-op) carries a fresh mtime and zero content change, so "recently touched" reads exactly like "has uncommitted work". Settle it with `git status --porcelain` / `git diff HEAD -- <paths>`, which compare content, before acting on a timestamp — the remedies (commit it, drop it, hunt an unknown writer) are expensive and all three are wrong when the content already matches HEAD.
- **A shared tool's npm `_npx` cache is a third artifact class — neither the executing copy nor the source.** When citing a shared tool's source for a claim (a line number, a regex, a constant, an accepted-token set), read it from the SOURCE REPO at a resolved ref (`git show origin/main:<path>`, stating the ref) — never from a path under a plugin cache or an npm `_npx` cache. For an npx-invoked CLI, a `_npx/<hash>/` directory is a leftover from a PRIOR invocation at whatever version the registry served THEN; npx re-resolves the registry's current version on every run, so the leftover answers neither "what is about to execute" nor "what is on `main`". Nothing on the reading path exposes a cache directory's version, so a wide gap is no protection: a `_npx` cache a full major version behind (`2.55.2` on disk against registry-latest `3.2.2`) produced wrong line numbers, and a version label taken from the running CLI's own error output rather than from the tree actually read — the two spliced together — before an independent reviewer, reading the source repo, caught both (#470).

(Case: `#231` recorded installed `2.39.0` while `main` carried the fix, and `#178`'s wrong-host 404 stayed reproducible after `!469` merged. Re-confirmed 2026-08-06/07 in the sharper form: the `2.41.1` upgrade landed mid-session, the on-disk CLI showed both `#246`'s `--close` verb and `#241`'s `diff_omitted` working, while the still-running MCP server reproduced `#246`'s original error **byte-for-byte** — `close` silently dropped, "at least one parameter must be provided". A VP-1 FAIL was recorded against code that had already been fixed, and reversed on evidence (`#241`, notes 56941→56961); separately, a second issue was nearly "corrected" to describe a gap that no longer existed, caught before the edit (`#246`, note_56960). The canary was green throughout, because the canary measures hooks, not which build answered a typed call. **The rule also worked when it was applied:** `#246` note_56781 probed the same defect against `2.40.5`, where the fix genuinely was absent, and recorded "fixed in source, unverified in deployment" rather than either verdict — the correct reading, and the re-probe it asked for is what later closed the issue.)

### A channel is only as informative as what its consumer can distinguish from it alone

Four measured 2026-08-10/11 defects share one shape, spanning domains that do not otherwise
overlap — an approve-endpoint status code (#326), a reviewer's schema-forced verdict field vs.
its posted note (#325, #328 — see `references/verdict-schema.md` #merge-authority for the fix
already shipped there), and a Stop-hook guard's remedy text (#329, still open at time of
writing): **a channel collapsed two or more distinguishable causes into one value, and its
consumer treated that value as if it named a single cause.** #328 names the shared property
directly: *"a channel carrying more meanings than its consumer distinguishes."*

- `mr_approve`'s bare `401` means bad-credential, already-approved, and not-permitted — a lane
  read cause (2) as cause (1), and that misreading reached a real decision: an owner ballot
  deferred a token rotation for five days against a credential valid until 2027-07-06 (#326).
- The orphan-guard's own reason line already computes "tracked edits, or untracked files that
  are not recognised lane scratch" — then prints the identical `git add + commit` remedy either
  way, including for scratch that must never be committed (#329).

**The remedy is never "read more carefully."** In every one of these cases the disambiguating
evidence already sat inside the reporter's own output — the lane's paragraph said "401" and "the
approval already stood" together; the guard's own diagnostic already named the distinction its
remedy then ignored. A careful reader missed it more than once in the same session while actively
hunting for exactly this shape — reading discipline does not scale, because it depends on every
future reader noticing what this session's reader, primed to look for it, still missed once. The
fix has to be structural: a parsed field the consumer cannot act without extracting
(`references/verdict-schema.md` #merge-authority), an idempotent response in place of a generic
error where the causes imply different next actions, or a remedy computed from the same value
the diagnostic already computed rather than a constant string keyed only to the trigger
condition.

**Before shipping any new status code, schema field, guard message, or log line: enumerate every
cause that can produce it.** If a consumer cannot mechanically tell those causes apart from the
channel alone, the channel is under-specified — split it before the first incident forces a
post-hoc read of prose that was there all along.

*Corollary, one level removed:* a derived artifact — a summary, a stale ballot's premise, a plan
built off the issue tracker instead of the approved spec — is not the thing it describes, for the
same reason a collapsed channel is not its own disambiguation: both substitute a lossy
restatement for the primary source. Read the primary source before acting on a note that
describes it (see "A vanished symptom is a lead, not a verdict" and "The installed plugin is not
automatically the code you merged" above for this law applied to other sources).

(Case: measured live 2026-08-10/11, session `4456b744-742d-4edd-9d09-90d74b7d82ce` —
`shared/claude-plugins` issues #325, #326, #328, #329, all `state: opened` as of this writing.
#328 states the unifying property verbatim; `references/verdict-schema.md` #merge-authority is
the shipped fix for the verdict-note instance of it. #326 and #329 remain open — cited here for
the general design property their shared shape demonstrates, not as evidence either is fixed.)

### A reviewer's `file:line` citation is relative to the head it reviewed, not `main`

A review note's `file:line` (or any "current behaviour is X") citation describes the state **at
the sha that review reviewed** — for an open MR, its branch head — never `origin/main`. Reading
it as a statement about main can invert a correct finding into a false retraction, and it is
exactly where the failure bites hardest: the branch is often ahead of main precisely at the path
the citation points to, because the MR exists to change that file.

- **Before acting on a citation — especially before contradicting or retracting a prior
  finding — resolve WHICH REF it describes.** The cheap oracle is a line-count or existence probe
  run against BOTH candidate refs: `git show origin/main:<path> | wc -l` vs
  `git show <branch-or-sha>:<path> | wc -l`. A citation whose line only exists on one of the two
  has already told you which ref it was.
- **Distinct from `references/accountability.md`'s "a subagent's report is a lead to verify,
  never a citable fact"** — that rule guards against promoting an unverified claim to fact. Here
  the report was accurate and independently reproducible; the reader supplied the wrong frame for
  an otherwise-correct citation. Re-running the same check against the wrong ref reproduces the
  misreading, not the truth.
- **Distinct from the "read the primary source, not a derived artifact" corollary (above)** — a
  reviewer's `file:line` citation IS a primary-source pointer, not a summary standing in for one.
  The fix is not "read the source instead of the note", it is "read the source at the ref the
  note describes".

(Case: measured 2026-08-12, `shared/claude-plugins`. `!542`'s reviewer wrote, present tense: "the
`mr_create` gate ALSO refuses a closing keyword in a pushed commit (mr.js:780 ->
close-gate.js:569, DENY proven by a 7-case matrix)". A lead read that as a statement about
`main`'s current state, concluded its own freshly-filed `#359` — "the closing-keyword ban is
enforced on MR title/description only, a commit body bypasses it" — was already handled, and
began drafting a public retraction. Checking first: `git show origin/main:...close-gate.js |
wc -l` -> 298 (the pre-`!542` state); `git show FETCH_HEAD:...close-gate.js | wc -l` -> 618
(`!542`'s branch). The branch adds `assertNoClosingKeywordsInPushedCommits`
(`gitlab/lib/close-gate.js:569`, called from `gitlab/lib/tools/mr.js:780`), whose own header
names itself the counterpart to "CI's agent-gate.mjs commit-range check (shared/claude-plugins
`#359`) — the LOAD-BEARING backstop for every MR in THIS repo". `!542` merged (commit `10dd1991`,
`close-gate.js` now 618 lines on `main` too); `#359` correctly stayed open — a retraction would
have deleted a true finding and the credit for the fix that closed it.)

### Prod-write discipline — gate the irreversible write *before* it can execute

- **Behavior-verify at the mechanism level, before you run it** — MUST name every field/collection/endpoint the client reads and confirm the write updates **each**, not just one plausible field.
- **Gate before you spawn, or two-phase it** — for any irreversible/prod write, EITHER decide the gate before spawning OR snapshot + dry-run then pause for explicit human go. You MUST NOT rely on a post-hoc stop.

### A `[PROOF]` block certifies the oracle RAN — not that its output was READ

The block's fields (`claim` / `oracle` / `output` / `source`) attest that a command was
issued and that this text came back. They do not attest that anyone compared the output to
what it was supposed to be, and the format cannot distinguish the two claims — so a result
that flatly contradicts the author's own expectation passes silently, formatted as proof.

- **State the expectation BEFORE running the oracle**, as a field of the block:

  ```
  [PROOF] claim:    <one line>
          oracle:   <exact command>
          expected: <what you predicted, and why — written BEFORE you ran it>
          output:   <verbatim>
          source:   <file:line | URL>
  ```

  `expected: >=1, a peer quoted this phrase as present at <sha>` against `output: 0` is a
  MISMATCH the author MUST resolve before publishing. Without the field, `0` is just a
  number.
- **This is a positive control applied to the REPORT rather than to the instrument**, and
  it costs one line. It converts a silent pass into a visible disagreement; it does NOT
  adjudicate the disagreement — that is still the span read, the wrap control, and the
  which-tree oracle below.
- **It does not catch a wrong expectation confidently held.** Nothing does. It catches the
  case where your own output already refutes you and you did not look.
- **A number that looks wrong against a cited prediction MUST be reconciled, never waved
  past** — that is precisely where a real defect hides. Reconciling it is cheap and the
  reconciliation belongs in the record.

(Cases, all 2026-08-14, one estate-wide effort: a lane published a `[PROOF]` block whose
third line read `0` for a phrase a peer had quoted verbatim as PRESENT minutes earlier —
the block certified the command ran, and the contradiction sat unexamined until the peer
re-read it. Separately, an issue was filed whose most load-bearing arm was labelled "the
decisive one" and had never been verified by its author. Separately again, a summary table
asserted a resolved split while its own discriminating experiment was still in flight.
Three distinct defects, one missing field.)

### An instrument's own scope is the unmeasured thing — and only two controls detect a blind one

A search that RETURNS is not a search that COVERS. Negative and positive controls prove an
instrument is neither vacuous nor mute, and **both pass unchanged on an instrument that is
structurally blind to the thing being looked for** — so a clear built on them reports the
blind spot as an absence. Five control classes, answering five different questions:

- **Negative — is it vacuous?** A pattern that MUST return empty. If it returns hits, the
  instrument matches everything and a clear from it means nothing.
- **Positive — can it speak?** A pattern that MUST return hits. Guards against a mute
  instrument, a wrong path, or an empty corpus.
- **Wrap — is the pattern blind to a split?** A phrase KNOWN to be broken across lines.
  Prose wraps at a width the author never chose, and a multi-word literal is invisible to a
  line-anchored match: join lines (`tr '\n' ' '`) and re-run. Note two distinct split kinds
  — a newline (line-join repairs it) and a SOURCE-STRING-LITERAL boundary (`', '` in an
  array of quoted lines, which line-join does NOT repair). **Replace such a seam with a
  SPACE, never delete it** — deleting concatenates the tokens either side and manufactures
  a fresh false negative. A single token is immune to both.
- **Known-positive-in-a-tree-where-the-thing-EXISTS — can it see THIS?** For any "X is
  gone" claim, run the identical oracle against a tree/artifact where X still exists (the
  merge-base, the installed release, your own injected prompt). **Same command, same
  phrase, two sources, opposite answers** is self-verifying. A generic positive control on
  a DIFFERENT phrase proves the tool works, not that it works on the phrase you care about.
- **Normaliser — does my repair actually repair?** Any transform built to defeat an
  artifact (line-join, seam-replacement, case-folding) needs its own known-positive. A
  no-op or over-eager normaliser returns a CLEAN SHEET, and clean is the answer you were
  hoping for; both other controls pass through it untouched.

Then, per CLAIM rather than per instrument — controls amortise, these do not:

- **Which TREE?** `git rev-parse HEAD` beside every file-derived claim. A perfectly-read
  sentence from a stale copy is still a wrong claim about the head. Note the rungs are
  distinct and have different oracles: repo tree (stable once taken) → published RELEASE →
  what is INSTALLED on this box (`ls <plugin cache>/*/` — a SAMPLE that moves under you) →
  what THIS session actually LOADED (for launch-injected text, your own prompt is
  first-hand; for hooks and skills there may be no in-shell oracle at all).
- **Which SPAN?** Read the sentence, not the clause. A correctly-quoted clause whose
  governing subject sits two lines above is a wrong claim with a right quote. Ordered:
  **which tree → which line → which span** — no use reading a sentence carefully if the
  pattern cannot match across the wrap that produced it, and none of it matters if you are
  in the wrong tree.
- **A boundary is a deliberate narrowing.** `\bword\b` silently excludes every inflection
  (`words`, `worded`), and all five controls above pass on it. **Substring for a SWEEP; a
  boundary only to CONFIRM a known literal.**
- **A refused or errored call is a FAILED EXPERIMENT, never a negative result** — a blocked
  search counted as a search performed is the same defect as a control reported green off
  an exit-127. Report the read-failure count beside any absence, or the absence is
  unfalsifiable. For paginated APIs, `-i` and `X-Total` / `X-Total-Pages` / `X-Next-Page`
  are a free completeness oracle; for diffs, read `git diff <merge-base> HEAD`, which is
  complete by construction, rather than an API that can silently truncate.

(Cases, all measured 2026-08-14 across five lanes: a liveness probe returned 4 matches, and
the same probe with a string that CANNOT EXIST also returned 4 — it was matching the
searcher's own shell. A doctrine sweep reported a clause REMOVED using a line-anchored
grep that returns `0` on the merge-base too, where the wording is present but wrapped. A
normaliser built to rejoin source-literal seams DELETED them instead, yielding `YOUR OWNWORK`,
a clean six-pattern sheet, and both controls green. A `\blead\b` sweep was structurally
unable to see `leads` — the plural being a live bolded sentence in the very file under
review — with negative, positive, wrap AND known-positive controls all passing. A phrase
sweep counted 3 sites where a single-token re-count found 7. In every case the conclusion
happened to survive; the method did not.)

### An outage is a claim about a tool call — cite the failed one, or report a hedge instead {#outage-needs-a-failed-call}

"X is down / unavailable / not in my roster" is a **live-system verdict**, and #proof-bar
applies to it exactly as it does to "the fix works". The evidence that discharges it is a
**call you actually made that failed**: the tool name, the shape of the arguments, and the
verbatim error or refusal. A SessionStart preflight banner does not qualify, nor a
PreToolUse hook warning, nor another lane having said so. Those are *priors*, and a prior
about an instrument is not a measurement of it: the preflight reads one oracle
(`claude mcp list` at launch), your tool roster is a different one, and they disagree —
**in both directions**.

**One non-call probe IS admissible, and only in its qualified form.** A `ToolSearch`
`select:` miss counts *when it names the id exactly as it appears in YOUR roster* —
`select:mcp__plugin_bdaya-defaults_socraticode__codebase_search`, not `select:codebase_search`.
The estate's own SocratiCode hook states the rule and the reason: *"a ToolSearch select:
probe counts only when it names the id as it appears in YOUR roster … A bare
select:codebase_search is NOT evidence - it reports no matching deferred tools even against
a healthy server."* So a bare-name miss is the WORST of both worlds — it looks like a
measurement and is structurally incapable of being one. Everything else in the "absence
from a listing" family is a prior, not evidence.

**Why this claim in particular, and not just any wrong claim.** An outage claim is the one
that **deletes its own falsifier**. An ordinary mistaken belief keeps meeting evidence:
you keep running the build, the test keeps failing, the wrong answer keeps getting
contradicted. (Not universally — a belief that stops you looking at all is the same shape,
and this estate has others; a stale checkout reporting ABSENCE for everything added
since you last pulled is one, and absence reads as good news so nobody re-checks it —
devops-aggregate `.claude/rules/00-session-pitfalls.md` #16.) But the instant you believe a tool is down you
*stop calling it*, so no evidence can ever arrive to correct you — and you switch to a fallback whose every answer
you must then mark unverified. One false outage silently downgrades a whole session's
epistemic tier, and nothing in the run will ever flag it.

The discharge is cheap and it is not optional:

- **Make one call.** If the tool is deferred, load its schema first and then call it. A
  single real invocation settles it in seconds; the banner never will.
- **A refused, errored or timed-out call IS the evidence** — quote it. That is the same rule
  as "a refused call is a FAILED EXPERIMENT, never a negative result", read from the other
  side: there the failure must not be counted as an answer, here the failure is precisely
  the answer, and both fail the moment the call is not made at all.
- **If you genuinely cannot call it, hedge — do not upgrade.** *"The preflight reported X
  unavailable; I did not attempt a call"* is honest and useful. *"X is down"* is a claim you
  have not earned, and downstream it will be read as measured.
- **Repeat the call before escalating.** One error is a retry; a second is an outage
  (`references/socraticode-playbook.md`). That file governs the CHANNEL an outage is
  escalated on — a lane has no `AskUserQuestion`, so its channel is the RETURN VALUE. This
  rule governs whether there is an outage to escalate at all; they compose, and neither
  substitutes for the other.
- **Naming the failing call is what makes the report actionable.** "SocratiCode is down"
  routes nowhere. "`codebase_search(query=…)` returned `<verbatim>` twice, 90s apart" names
  the backend, the surface and the symptom, and a lead can act on it.

(Cases, measured 2026-08-23 in one session, lane `gw3c` on `windows-pc`, both from the SAME
oracle and pointing OPPOSITE ways. Its SessionStart preflight reported
`plugin_bdaya-defaults_gcp` among the servers "NOT REPORTED AVAILABLE" — and
`gcp_secret_list` answered normally minutes later, after which `gcp_secret_write` created a
secret. The same preflight, plus a hook re-firing on every Bash call, reported SocratiCode
unavailable for the whole session; BOTH fresh-context reviewers spawned inside that same
session issued a qualified `codebase_search` that returned real indexed results, one
recording its verdict as `Method: socraticode` rather than grep-derived and the other
reporting the banner as "wrong in the pessimistic direction this session" unprompted. This rule was commissioned
because two lanes that day reported an outage neither had observed — stated as the
commissioning brief reports it, not as something measured here. The hook's own text already
concedes the point: *"that oracle is a WARNING signal, not proof of your tool roster - it has
disagreed with the real roster in both directions"* — so the banner is a reason to TEST,
never a citation.

And note WHEN these were measured. shared/claude-plugins#172 — "a backend down at session
start silently blinds that session for its whole life, and a hook tells the agent to use the
missing tool" — is **CLOSED**, fixed in `bdaya-defaults-v2.27.0` on 2026-08-02. Its fix is
what produced the hedged banner quoted above, and that fix is good. The two false negatives
here are from three weeks AFTER it, which is the whole point of this section: the banner is
now honest about its own uncertainty, and an agent that reads it as a verdict anyway is
making the error at the reading end, where no upstream fix can reach.)

### An oracle that probes two endpoints cannot verify the transition between them

Two `git grep`/read calls at two different commits each prove a STATE, never the EDIT a
transition claim asserts happened between them. If other, unrelated commits touched the same
symbol in between, both endpoint reads still return true values and the endpoints still differ
exactly as the claim predicts — the composed claim ("commit `X` bumps A from 5 to 8") passes an
oracle that never actually read `X`'s own diff.

- **The tell: when an oracle's output value equals a number already written in the claim's own
  prose, ask whether the oracle MEASURED that number or whether both merely quote the same
  constant.** A reviewer who reruns the shipped check and gets back exactly the two numbers the
  prose already printed reads the coincidence as corroboration — not as the giveaway that the
  check never looked at anything in between.
- **For a transition claim ("commit X changes A to B"), read the commit's own diff** —
  `git show <sha> -- <path> | grep -E '^[+-].*<symbol>' | grep -vE '^(--- (a/|/dev/null)|\+\+\+ (b/|/dev/null))'` — never two
  endpoint states. Drop the `---`/`+++` headers with a second `grep -v`, NOT by tightening the
  indicator to `^[+-][^+-]`: that also discards every changed line whose own content begins with
  `-` or `+` — every top-level markdown bullet, every `--flag` in a sample, and the `++`
  resolution line of a combined diff — turning two lines of visible noise into a silent empty
  result, which is this section's own failure again. Match the header's full shape rather than
  the bare `^(--- |\+\+\+ )` prefix: a REMOVED line whose content starts with `-- ` renders as
  `--- ` and the loose form eats it (on a combined diff where a bullet is dropped from both
  parents, it returns *nothing*). **No text filter here is total, and the two residuals point
  opposite ways:** content literally beginning `-- a/…` or `-- /dev/null…` is still dropped, and
  under `--no-prefix` / `diff.noprefix=true` / custom `--src-prefix` the headers lose `a/`/`b/`
  and survive as noise. Prefer this form anyway — its match set is a strict SUBSET of the loose
  one, so it can only ever drop fewer content lines and leave more headers: it degrades into
  visible noise a reader discards, never into absence a reader cannot see.
  Endpoints prove the value differs somewhere across the range; only the diff proves this commit
  is where. **When `<sha>` is a MERGE, use `git show -m --first-parent <sha> -- <path>` or
  `git diff <sha>^1 <sha> -- <path>`**: `git show`'s default combined diff suppresses hunks that
  match one parent, so on a clean merge it prints *nothing* and the piped `grep` reads as "this
  commit did not touch the symbol" — this section's own failure, wearing the remedy's clothes.
  (Measured: on one estate's `--no-ff` merge, `git show <merge> -- <path>` emitted 0 bytes where
  the first-parent diff showed 7 changed lines (`git diff --numstat` => `6  1`; a bare
  `grep -cE '^[+-]'` says 9 because it counts the two file headers — the same artifact this
  bullet's own pattern guards against). On a *conflicted* merge the combined diff does
  show the resolution hunk, so it works sometimes and fails silently otherwise — the reader
  cannot tell which case they are in.)
- **For a "this commit introduced/added X" claim, use `git log -S'<X>' -- <path>` with `X` the
  introduced LITERAL** — `-S` is the pickaxe: it reports commits where the *count of occurrences*
  of `X` changed, i.e. where it was added or removed, so it answers "when did this string appear"
  and not "what happened to it since". **For "what CHANGED X", use `-G'<regex>'`.** `-S` on a bare
  symbol name returns only the commit that introduced the symbol and silently omits every later
  commit that changed its value — the same reassuring-silence failure this section names.
  (Measured on a 4-commit range bumping one constant 5→6→7→8: `-S'<symbol>'` returned **one**
  commit, the introduction; `-G'<symbol>'` returned **all four**; `-S'<symbol> = 8'` correctly
  returned only the commit that set 8.)
- **A count-based oracle backing a claim of N named artifacts must attribute each hit to a
  named artifact, not just match the total.** A `grep -c` built from a multi-alternative regex
  can hit the right count while missing one alternative entirely and double-hitting another —
  the sum coincides with N while covering fewer than N of the N things the claim lists.

(Case: measured 2026-08-19 on a client `.NET` backend MR. A doc comment asserted a
version-getter bump "from 5 to 8" at a cited commit, backed by two `git grep` calls at two shas
three months apart — both commands true, both reproducible, and both silent about two unrelated
bumps sitting between those shas by a different author; the cited commit's own diff was
actually `-7/+8`. The same sentence also claimed the commit added three named artifacts, backed
by one `grep -c` over a two-alternative regex that returned `3` — matched by luck, from two
genuine hits plus a second hit on one alternative, while the third artifact's own name matched
neither branch. A full adversarial review round re-ran both shipped oracles while verifying an
unrelated correction to the same sentence; each returned the number the prose already asserted,
and each read as confirmed. Only reading the commit's own diff caught the version-bump error;
the label miscount was never caught by rerunning either oracle at all — a later round caught it
only by reading the diff directly.)

**See also:** `internal/guard-structurally-incapable-of-detecting-its-own-defect.md` in
`shared/knowledge-base` — the closest prior art: a guard can be green, thorough, and
mutation-proven and still be structurally unable to detect what it exists to prevent, when a
free variable in the claim was never varied or a control fails toward the reassuring answer.
This entry is that same failure surviving full adversarial review one level further out — not a
test guard but an evidence command shipped beside a claim to prove it — with the specific
numeric-coincidence tell that lets a careful reviewer wave it through.
