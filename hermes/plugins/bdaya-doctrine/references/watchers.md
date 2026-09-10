# Watchers & self-driving waits {#watchers}

> Shared `bdaya-core` doctrine reference. Deep home for the constitution's `#router`
> watcher row. bdaya-work's `references/watching.md` keeps the GitLab reap mechanics and
> points here for the generic law. Normative keywords follow BCP 14 (RFC 2119, RFC 8174).

Waiting on something long-running (CI pipeline, MR merge, deploy, background build/test) — **you MUST NOT end your turn telling the user to "ping me for status."** You MUST arm a watcher that wakes you when it finishes.

- **One completion, ≤~15 min** → prefer the **`Monitor`** tool armed with a loop that emits and exits on the terminal condition over a `Bash` `run_in_background` sleep-poll loop for the same wait. **Observed 3× on Windows** (session `44c7412e`: killed bg tasks `bv614n9u7`, `b21h0bp9c`, `bpms8mhfp` — `b21h0bp9c` was killed before its queued retry fired, output file EMPTY) that the harness reaped the backgrounded poll loop before it reached its exit condition, while two equivalent `Monitor` loops in the same session ran to completion (~5 min and ~9 min: `bpcv1xxh6`, `b60142dub`) and delivered the terminal event. `Bash` `run_in_background` remains sanctioned when `Monitor` doesn't fit the wait's shape; a sleep-based poll loop is the specific shape that was reaped. You MUST NOT foreground-`sleep` to wait, and any one-shot action gated on the wait (a job retry/replay) MUST run in the FOREGROUND before arming the watcher — never stage it to fire from inside a backgrounded sleep chain.
- **Per-event streaming** → the **`Monitor`** tool, printing each new terminal status, exiting when none are pending.
- **Self-paced re-checks** with no single signal → `ScheduleWakeup` in a loop.
- **25+ min waits** → neither `Monitor` nor `run_in_background` is proven reliable — MR !135 observed a `Monitor` loop ALSO killed past that horizon, so the ≤~15 min preference above does NOT extend past it. Fall back to WATCH-1's detached-process + logfile-polling recipe below.

**Non-negotiable — silence is not success.** The exit/emit condition MUST match **every** terminal state (`success` AND `failed | canceled | timeout | skipped`), not just the happy path — *if this crashed now, would my watcher fire?* Poll ≥30s for remote APIs. The harness auto-resumes you when background agents/workflows/subagents finish — only poll for **external** state (CI, deploys, remote queues).

Watcher/heartbeat law for bdaya-work runs: skills/bdaya-work/references/watching.md.

## WATCH-1 — Windows detached long-running jobs (Start-Process + logfile polling)

On Windows there is no `run_in_background` PTY to tail and no POSIX job control, so a
long-running detached job (a build, a soak test, an `aspire run`) is watched by **logfile
polling**, not stream capture:

- **Launch detached** with `Start-Process` (or `cmd /c start /B` when the child must
  survive the parent shell's exit — a bare `Start-Process -WindowStyle Hidden` still dies
  with the parent's Windows job object), redirecting output to a logfile and keeping the
  PID: `Start-Process -FilePath … -RedirectStandardOutput out.log -RedirectStandardError err.log -PassThru`.
- **Sample progress** by polling the logfile tail *and* the process table on an interval —
  `Get-Content -Tail 40 out.log` for output, `tasklist /FI "PID eq <pid>"` (or
  `Get-CimInstance Win32_Process -Filter "ProcessId=<pid>"`, the modern replacement for the
  deprecated `wmic process`) for liveness. The watcher's terminal condition MUST fire on
  **both** "process gone" AND a failure marker in the log — never just the success line.
- **Tail-buffering corollary:** a child that buffers stdout (many .NET / Python runners do
  when not attached to a console) flushes late, so the logfile can lag real state by
  seconds-to-minutes. Treat an empty recent tail on a still-alive PID as *unknown*, not
  *idle*; confirm terminal state from the exit code (`$proc.HasExited` / `$proc.ExitCode`),
  not from the log going quiet.

Stack-specific instances of this generic recipe: the Aggregate's
`.claude/rules/01-aspire-windows-detach.md` (`aspire run` vs `aspire start` watchdog) and
`backend-rules` (Aspire detach on Windows).

## WATCH-2 — a firstSeen-windowed prod sweep cannot prove prod CLEAN

A prod-error sweep whose query is windowed on **first-seen time** (`firstSeen:>T`)
structurally excludes every issue first seen *before* `T` — including a long-standing issue
that is actively **escalating right now**. So a green result from a firstSeen-windowed sweep
is NOT proof prod is clean: the lead MUST NOT conclude "prod healthy / no regressions" from a
firstSeen-windowed query alone (an escalating pre-existing issue is invisible to it). To
actually clear prod, also sweep on **event-recency / last-seen** and on escalation state (an
issue re-firing or trending up regardless of when it was born), then reconcile the two. The
query-semantics specifics for the monitoring backend (the `firstSeen`/`lastSeen` windows and
the `substatus:escalating` filter) are a KB fact; this rule is the discipline that holds
whatever the backend.

## WATCH-3 — remote-shell + exit-code hygiene for shell watchers

A shell-based watcher is only as trustworthy as the **shell it runs in**, the **exit
code it returns**, and the **filter it keys on**. Each can silently invert the result —
report a healthy thing as failed, or a broken thing as fine — while the watcher still
*looks* armed. Three rules keep a shell watcher honest.

- **(a) A remote watcher body MUST run under `bash -s`, not the remote login shell.** When
  you drive a watch over `ssh <host> '<script>'`, that script runs in the remote user's
  **login** shell — on macOS the default is **zsh**, which does NOT word-split unquoted
  parameter expansions (`SH_WORD_SPLIT` is off by default). A POSIX idiom that relies on
  splitting — `for pair in $list; do set -- $pair; name=$1; state=$2; done` — silently
  breaks: zsh assigns the whole `"alpha running"` string to `$1` and leaves `$2` empty, so
  the watcher reads a false "empty / missing / not-ready" state for **every** item and can
  report a passing run as failed. Force bash for the remote body with a quoted-heredoc
  `bash -s` (verified on `ssh macos`: zsh → `1=[alpha running] 2=[]`; `bash -s` →
  `1=[alpha] 2=[running]`):

  ```bash
  ssh <host> bash -s <<'EOF'
  for pair in $list; do set -- $pair; name=$1; state=$2
    [ "$state" = running ] || echo "MISSING: $name"
  done
  EOF
  ```

  The **quoted** `<<'EOF'` also stops the LOCAL shell from expanding `$1`/`$state` before
  they reach the remote bash. (The Bash tool's own commands already run under bash — this
  hazard is specific to the REMOTE shell.)

- **(b) A watcher's final statement MUST NOT be a bare `cond && action` — end it `|| true`
  or use an explicit `if`.** A `run_in_background` watcher's process exit code IS its
  terminal status to the harness / task system: a non-zero exit marks the task **failed**
  even when the watched thing SUCCEEDED. A watcher ending `[ -z "$missing" ] && echo
  "TIMEOUT"` leaks the FALSE-branch code — on success `$missing` is non-empty, so
  `[ -z … ]` is false (exit 1), `&&` short-circuits, and the script exits 1 with nothing
  wrong (verified: the non-empty/success path exits 1; `|| true` and the `if` form both
  exit 0). Neutralize the last statement:

  ```bash
  # BAD  — exits 1 whenever $missing is non-empty (the success case)
  [ -z "$missing" ] && echo "TIMEOUT: $missing"
  # GOOD — trailing guard, or an explicit branch (a false `if` still exits 0)
  { [ -z "$missing" ] && echo "TIMEOUT: $missing"; } || true
  if [ -z "$missing" ]; then echo "TIMEOUT: $missing"; fi
  ```

  This is the exit-code corollary of the top-of-file law (*silence is not success*): here
  the watcher fired correctly, but a stray exit code reports its success as a failed task.

- **(c) Validate a filter against a live positive before you trust it — and before you stop
  the watcher it replaces (arm-v2-before-stop-v1).** A watcher / `Monitor` filter (a jq
  expression, grep pattern, or kubectl selector) that is malformed or mis-targeted matches
  NOTHING and fires never — a blind watch that still looks armed (a jq compile error inside
  a `Monitor` filter degrades exactly this way). Before a filter becomes your ONLY signal
  you MUST confirm it EMITS on a **known-present positive**: a currently-true instance you
  can see by hand. When a new filter (v2) is meant to replace a working one (v1), keep v1
  armed until v2 has fired on that live positive — never stop the proven watcher on the
  unproven one's promise.

## WATCH-4 — an attribution field must be PROVEN to discriminate, not assumed to

Before filtering or attributing query results to one population among several
(environment, release, tenant, host, …) by a tag/field, verify EMPIRICALLY that the
field's value actually differs across those populations: group by it first and confirm
each population you mean to separate produces a distinct value. A field that reads the
SAME constant value across every population partitions nothing — but the query still
returns a plausible, non-empty result set, so **the failure is a confidently-wrong
answer, not an empty one**: it looks like success and carries no signal anything is
wrong. Never trust a field as a discriminator on the strength of its name or its mere
presence in the schema alone.

(Verified live 2026-08-16 against a Sentry organization where every environment — dev,
staging, and prod — carried the identical `release` tag. A query filtered on
`release:` instead of `environment:` silently blended all three environments' events
under the one filtered label, which read as a sustained prod incident until the same
query was re-run grouped by `environment` and corrected. The specific incident this
generalizes from is recorded internally, cited by KB reference rather than repeated
here — this rule is the reusable discipline, not the incident.)

**Corollary — the same proof obligation applies to a persistent-absence verdict.**
WATCH-3(c)'s "validate a filter against a live positive before you trust it" already
covers this: before reporting a query's ZERO as "this never happened," run the
IDENTICAL query against a population known to produce a non-zero result (another
environment, a wider window) to prove the query mechanism is even capable of returning
the falsifying answer. A zero from a query never shown able to return non-zero is
unproven, not confirmed — record the positive-control result alongside the zero, not
just the zero.
