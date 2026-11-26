# CLI dashboard for pipeline and agent observability

## Users and questions

- Engineers watching a goal can see its pipeline, concurrent jobs and retries,
  the stage in flight, and the agent and model serving each invocation.
- On-call engineers can distinguish quiet work, stale telemetry, a missing process,
  checkpoint waits and failures, then inspect gate evidence and attribution.
- Authorized reviewers can see what needs their decision, why, and how to act.
- Operators can see today's recorded spend and tokens, engine limits and the age
  and coverage of reported quota and cooldowns.

## Views and navigation

The header shows source health, refresh age and pending approvals. At 80×24 use
one pane with drill-down. At 160×48 keep the master list and selected detail
side by side. Sketches show populated rows within each viewport, with padding
and spare rows omitted. Status words work in monochrome. Below 80 columns hide
secondary columns first and wrap details.

1 overview, 2 pipeline, 3 jobs, 4 approvals, 5 quota/cost, 6 incidents.
j/k or arrows move, Page Up/Down scroll, Tab changes focus, Enter opens the
selection, Esc goes back, ? opens help, r refreshes and q quits. Preserve
selection by stable key across refresh and resize. / edits an AND filter by
backend, goal, state, role, agent/model or literal text. Clear the filter with
Esc in the filter editor. In detail, l opens events, g opens gates and p opens
the linked pipeline. In events, c cycles all/command/message and f toggles
follow. Scrolling back pauses follow. In approvals, a and x open confirmation
for the selected pending record. Reject requires a reason.

### Overview

80×24
~~~
Overview   Both sources | updated 2s ago | 1 approval
Running 3   Queued 2   Waiting 1   Failed today 2
> G17   Implement running / 2 concurrent jobs
  G18   Brief waiting / review required
  ext:J9   Running / goal unknown
Recorded today $0.42   Coverage 6/9 completed jobs
Enter Open   2 Pipeline   3 Jobs   4 Approvals
~~~

160×48
~~~
Overview   Pack current 2s | External current 1s | 1 pending approval
Goals and standalone jobs                 | G17   Implementation in flight
> G17  running    2 active                | pack:R17  implementer / worker-a   model-1   last event 2s
  G18  waiting    1 approval              | ext:J12   verifier / worker-b      model-2   quiet 18s
  J9   running    goal unknown            | Next dependency   gate after implementation
                                          | Today $0.42 recorded / 6 of 9 completed jobs
Enter Open selected   Tab Focus preview   4 Approvals   5 Quota/cost   6 Incidents
~~~

### Pipeline per goal

80×24
~~~
Pipeline   G17   Pack + linked external job
[plan OK] -> [brief OK] -> [implement RUN]
                            +-- ext:J12 verifier RUN
                            +-> [gate expected] -> [review expected]
Enter Selected job/stage   3 Jobs   Esc Back
~~~

160×48
~~~
Pipeline   G17                                           | Selected stage   implement
[plan OK] -> [brief OK] -> [implement RUN]                | pack:R17/03   worker-a / model-1
                              +-- [verifier RUN] ext:J12 | Last event 2s ago   Started 11:02
                              +-> [gate expected] -> [review expected] -> [pre-merge expected]
Retry path when observed   review -> fix #1 -> gate #2 -> review #2
Enter Open observed job   Tab Focus stage evidence   Esc Back
~~~

### Jobs and filters

80×24
~~~
Jobs   / status=running   3 matches
  Job       Backend  Status   Agent/model       Age
> R17/03    pack     running  worker-a/model-1  2m
  J12       external running  worker-b/model-2  1m
  J9        external running  worker-c/model-1  8m
/ Filter   Enter Detail   r Refresh
~~~

160×48
~~~
Jobs   / status=running   newest activity first                                  | Selected pack:R17/03
  Job        Goal  Role          Backend  Status   Agent     Model    Last event | Run R17   Step 03
> R17/03     G17   implementer   pack     running  worker-a  model-1  2s         | Started 11:02:00
  J12        G17   verifier      external running  worker-b  model-2  18s        | Gate pending
  J9         ?     ?             external running  worker-c  model-1  6m         | Usage unknown
/ Filter   Enter Detail   Tab Focus preview
~~~

### Job detail

80×24
~~~
Job   ext:J12   Goal G17   running
Agent worker-b   Model model-2   Attempt 2
PID verified alive   Last event 18s   Duration 1m
  1  failed    exit 1
> 2  running   result pending
Gate pending   Changed files unknown   Cost unknown
l Events   g Gates   p Pipeline   Esc Jobs
~~~

160×48
~~~
Job detail   ext:J12   G17   running                     | Selected attempt 2
Agent worker-b / model-2   started 11:03:00               | PID verified alive   Last event 18s
  1  worker-b / model-1    failed    exit 1               | Changed files unknown   Verdict pending
> 2  worker-b / model-2    running   exit unknown         | Brief and reply available (restricted)
Pipeline verifier   Gate pending                         | Usage unknown   Attribution unavailable
l Events   g Gates   p Pipeline   Esc Jobs
~~~

### Live events

80×24
~~~
Events   ext:J12 attempt 2   command only   following
11:03:28  started    command   call 17
11:03:29  completed  command   call 17  exit 0
c All/command/message   f Follow   PgUp Scroll
~~~

160×48
~~~
Events   ext:J12 attempt 2   command only   following             | Selected event
Time          Phase       Kind      Call  Summary                 | Call 17   exit 0
11:03:28      started     command   17    Command started         | Source attempt 2 / stdout
11:03:29    > completed   command   17    Command finished         | Command content restricted
c Cycle filter   f Follow   PgUp Pause and scroll   Esc Detail
~~~

### Gate output

80×24
~~~
Gates   ext:J12   attempt 1   gate/1.log
Outcome fail   Source runner result
> Test check failed   exit 1
  <redacted diagnostic excerpt>
j/k Scroll   Esc Detail
~~~

160×48
~~~
Gate receipts   ext:J12                       | gate/1.log   attempt 1
> 1  fail    exit 1                           | Test check failed
  2  pending                                  | <redacted diagnostic excerpt>
                                              | Result: failed   Source: runner result
Enter Select receipt   Tab Focus output   Esc Detail
~~~

### Approvals

80×24
~~~
Approvals   Pending 1   Pack
> A17   brief   blocking   G18   waiting 8m
Owner tech lead   Brief lint passed
a Approve   x Reject   Enter Evidence
Confirm G18 / brief / A17   [Cancel]   Approve
~~~

160×48
~~~
Approvals   Pending 1   Decided 4                 | A17   brief   blocking
> A17    pending   brief     G18   8m             | Required role   tech lead
  A16    approved  pre-merge G15   26m            | Evidence   brief lint passed   fingerprint 7b2e...
                                                  | On approval   operator can resume separately
a Approve   x Reject   Enter Evidence             | Confirm G18 / brief / A17   [Cancel]   Approve
~~~

### Quota and cost

80×24
~~~
Quota/cost   Today 00:00..now local
Pack       $0.42 recorded   6/7 priced
External   unknown          0/2 priced
> worker-b  cooldown until 11:14   age 40s
  worker-a  remaining quota unknown
Enter Source details   Esc Back
~~~

160×48
~~~
Quota/cost   Today (local timezone)                     | Selected pool entry   worker-b
Backend    Jobs  Priced  Known USD  Input/output tokens | Cooldown until 11:14   sampled 40s ago
Pack          7       6      $0.42  29,000 / 6,100      | Remaining quota unknown
External      2       0          ?  unknown             | Pack context/time limits shown separately
Enter Source details   Tab Focus pool   Esc Back
~~~

### Incidents and attribution

80×24
~~~
Incidents   Today   2 failures
> R15   gate failed   TOOL (suspected, medium)
  J11   exit 1        unknown
Evidence  step 03 / gate receipt / exit 1
Enter Evidence   Esc Back
~~~

160×48
~~~
Incidents   Today                                  | R15   Gate failed   step 03
> pack:R15  gate failed  TOOL suspected (medium)   | Evidence   gate receipt, line 12
  ext:J11   exit 1       unknown                   | Secondary ENV   Confirmation pending
                                                   | Memory used   2 entries / provenance available
Enter Job and evidence   Tab Focus attribution   Esc Back
~~~

## Data contract and adapters

Adapters feed an in-memory store that produces one redacted snapshot contract for
all renderers. A JobRecord is a logical execution with expandable invocation or
attempt children. Keys contain backend, configured source and native ID plus
child ID if applicable. Missing fields are null. Join goals, dependencies and
approvals only through documented IDs or verified links. Unlinked jobs get a
flat timeline. Step order provides display order. Render dependency arrows only
from explicit links or known workflow semantics. Label unseen stages as expected
and render observed retries, parallel branches and skips explicitly.

| Type | Fields |
| --- | --- |
| JobRecord | Key, parent/goal/dependency keys, source and native status, normalized state, role, agent/engine, requested and observed model, start/end/duration, exit/verdict, changed files, gate and approval refs, usage and cost provenance, attribution and memory refs, last event and observation, process evidence, children |
| StepEvent | Job/attempt key, stream generation and byte offset, optional source timestamp, observed time, kind, started/completed phase, native item/call ID if present, exit code, redacted summary, source location |
| Approval | Source and UUID, checkpoint ID and kind, state, subject, blocking/hard-stop, required role, evidence fingerprint, job and continuation links, created/decided times, hashed or policy actor, redacted note |

Normalize execution to queued, running, waiting for approval, ready to resume,
succeeded, failed, refused, rejected, cancelled or unknown. Terminal receipts
take precedence over stream and PID hints. A blocking checkpoint maps a paused
run to waiting or ready to resume. A terminal checkpoint is displayed alongside
the execution outcome. Keep activity inference and health separate from status.

### Pack adapter

| Input | Mapping |
| --- | --- |
| `runs/<run-id>/run.json` | run_id, kind, task, started, finished, outcome and engines feed the parent record. |
| `steps/NN-<role>-<engine>.meta.json` | Role/engine/model/status, duration seconds or milliseconds, token counts and cost from the supported aliases. Stem supplies fallback identity. Prompt or stream may exist before final metadata. |
| Matching .jsonl | Reuse event interpretation in runner/devpack_logs.py for message and tool variants. Command execution maps to command, other tools to tool. Preserve file order. |
| Matching .prompt.md, NN-gate.txt, transcript.md | Prompt and transcript availability in detail. Read bounded redacted gate output and use run/step receipts for gate state. Generated transcript is a snapshot. |
| `.devpack/queue/<id>.{request,response}.json` | A complete request lacking a response is queued or active/unconfirmed. Match the broker's copied request filename under a run or a validated response run_dir. Merge linked records once and ignore temporary requests. |
| `<state_dir>/approvals/{pending,decided}/<id>.json` | Checkpoint decision, evidence and subject. Decided record wins during transition. |
| `<state_dir>/runs/<id>.json` and `<id>.consumed` | Continuation keyed by approval ID. Show approved/resumable only with a valid unconsumed continuation. Withhold the saved payload. |
| attribution.json and step/run memory provenance | Causes, confidence, evidence and human confirmations. Use memory IDs/counts and proposal states where recorded. |

A dispatch can run independently of the broker. The serial broker retains request
and response artifacts. A submit timeout never establishes a job outcome and
request age alone cannot establish that processing has started.

### External job runner adapter

| Input under `<jobs-root>/<job-id>/` | Mapping |
| --- | --- |
| request.json, brief.md | Directory ID, requested identity and explicit goal metadata when available. Brief availability in detail. |
| result.json | status, exit_code, agent, model, started, ended, duration_s, attempts, changed_files and verdict. Attempt result fields override filename hints. |
| pid | Candidate local PID. Verify host and process identity before believing liveness. |
| `attempts/<n>-<agent>-<model>/stdout` | thread.started maps to lifecycle. item.started/completed with command_execution or agent_message maps to command or message. Pair item IDs within the attempt. |
| Attempt stderr, reply.md, `gate/<n>.log` | Diagnostic output, reply availability and ordered gate receipts. Join gates to attempts where the numbering contract establishes a link. |
| jobs, `show <id>`, pool | Files cover jobs and detail. Pool requires a vetted local snapshot or an explicitly verified local-only machine-readable command. |

A missing result means final state is unknown. A verified live process or active
attempt can support a running state. Approval, attribution, usage and goal links
are unavailable until a source provides mapped evidence. A linked pack approval
is actionable only through the pack adapter.

### Tailing, scale and health

- Poll active streams and approvals every 1s, active metadata and pool every 3s,
  and discover job/run directories every 5s, backing off to 30s when idle. Stop
  active polling for finalized, drained jobs. Manual refresh wakes all scans.
  Optional inotify wakes the same reader. Periodic discovery reconciles missed
  notifications and mounts with limited notification support.
- Track binary offsets by device/inode and generation. Buffer partial UTF-8 and
  JSONL bytes until newline. On replacement or truncation reset offset and partial
  buffer, emit a gap marker, and distinguish new event IDs. Bound any drain of the
  old descriptor. Count malformed or oversized complete records without dumping
  raw text. On a terminal stream allow a bounded valid final JSON record without
  newline after two stable polls.
- Parse changed snapshots off the UI thread. On transient parse failure keep the
  last good snapshot with its age and error badge. Distinguish empty, inaccessible
  and stale sources. Keep open details marked source-removed when a file vanishes.
- Discover with scandir in batches of 500, prioritize active and selected jobs,
  keep 10,000 recent terminal summaries and page older ranges on demand. Keep
  1,000 events per selected stream, cap records at 256 KiB and spend up to 2 MiB
  of event reads per tick round-robin. Occasional full reconciliation catches
  inactive entries becoming active. Report partial totals and unread backlog.
  Target keyboard response under 100 ms in a local fixture with 10,000 historical
  runs and 100 active jobs. Coalesce snapshots when the renderer falls behind.
- After 5 minutes without meaningful activity, mark running jobs quiet. Show
  suspected stuck/orphaned when the process is verified gone and there is no
  terminal receipt. Use local host and process start identity where recorded to
  detect PID reuse. A remote root, PID-only record or permission denial leaves
  identity unverified. Quiet with a live or unverified PID remains quiet. Exclude
  known waits, queued jobs and terminal jobs. Use monotonic polling and UTC
  timestamps with local display.

### Accounting and incidents

Use calendar-day completion time in the local timezone. Count each independently
measured invocation or retry once. Prefer an authoritative scoped run total where
available, otherwise sum step measurements. Keep undated breakdowns in a labeled
completion-day total. Track explicit numeric zero separately from missing values
and parser defaults. Show known spend, priced/eligible counts, tokens with their
own coverage and scan completeness. Configured context/time limits from
runner/engines.example.toml's schema are capacity hints. Remaining quota and
cooldown require dated local evidence. Rate-limit errors belong in incidents.

Show the latest valid human confirmation alongside heuristic evidence, timestamp
and confidence. Use BRIEF, CONTEXT, SKILL, TOOL, MODEL and ENV, with unknown when
absent. Read existing attribution.json first. On-demand pack analysis may use
analyze_run_dir with write=False and bounded inputs. Existing analyze/attribute
CLI writes stay outside the dashboard. Reuse runner/devpack_memory.py's read-side
semantics for memory links. Show provenance without loading raw memory bodies
into search or invoking a memory mutation directly.

## Runtime, CLI and config

Use Python 3.10+ stdlib for the data layer, plain text, JSON and curses. Curses
handles keyboard focus, resize and differential painting with noutrefresh and
doupdate. Restore terminal state on exit. Degrade to plain for pipes, missing
curses, unsupported terminfo or TERM=dumb. In v2 an operator-selected richer UI
can use an already installed library. It shares snapshots and action semantics
and falls back to curses, then plain. The design uses the DB0 research patterns
for master/detail, adaptive refresh and GenAI-style agent, operation and usage
fields. There is no telemetry exporter or mandatory dependency.

~~~
devpack_dash.py [--backend pack|external|both] [--runs-root PATH]
                [--state-dir PATH] [--jobs-root PATH] [--plain|--json] [--once]
                [overview | pipeline GOAL | jobs [--filter QUERY] | show JOB
                 | tail JOB [--kind all|command|message] | gate JOB
                 | approvals [--status pending|decided|all] | quota | incidents
                 | approve ID [--note TEXT] | reject ID --note TEXT]
~~~

Default to overview and curses on a usable TTY. Plain/JSON return one snapshot
unless the explicit tail command is used. --once takes one snapshot or drains a
bounded initial tail page, then exits. Without an explicit renderer --once uses
plain. JSON carries a schema version, observation time, source health and nullable
fields. Streaming JSON uses one record per event or notice. Qualify job selectors
by backend and reject ambiguity. Exit 0 for successful reads and recorded
decisions, 1 for action/read failure or all selected roots unavailable, 2 for
invalid arguments/config. A job failure is data in a successful read.

Use .devpack/project.toml [dashboard] for backend, runs_root, state_dir, jobs_root,
poll intervals, quiet threshold, history limit and optional renderer. CLI flags
override config, then defaults apply (pack, project runs/, standard pipeline
state resolution, no external root until configured). Resolve relative config
paths against the project config directory. Validate known keys with the existing
Python 3.10-compatible parser. Resolve write paths independently from trusted host
pipeline configuration. Project display settings cannot grant decision authority.

## Security and decisions

- Open bounded regular files read-only under configured roots using
  directory-relative no-follow opens where supported. Scope known filenames and
  reject escapes and symlinks. Treat payload paths and text as untrusted. No
  network, queue writes, command execution from logs, or signal delivery.
- Allowlist fields, remove terminal control sequences and controls, and use
  runner/redaction.py for every display and output, including errors, filter
  results and JSON. Withhold raw prompts, briefs, replies and command/message
  bodies by default. Use only bounded redacted excerpts and trusted literal
  patterns. Redact complete logical records before excerpting. Drop oversized or
  incomplete records where safe redaction cannot be established. Fall back to metadata-only for sources that cannot meet display policy.
  Keep raw text out of snapshots, caches and exports.
- Decisions go through trusted host runner/devpack_approve.py semantics, including
  validation, hashed OS-user audit identity, provenance and existing memory
  side effects. Tech leads own plan/brief/escalation, tech leads or safety managers
  own deviations, security/data owners own security manifests and human reviewers
  own pre-merge/memory. Authorized CI may use the existing operator path where
  policy permits. The hash supports audit; host permissions and roles authorize.
- Refuse actions within agent sandboxes and through broker forwarding. Separate
  agent and operator accounts and permissions. Keep trusted action entry points
  and state outside agent mounts. A TTY or caller-controlled environment marker
  alone cannot establish operator identity. Disable actions where an agent shares
  the operator's state write permissions. Attribute existing deterministic
  auto-approvals to policy; the dashboard issues only explicit operator decisions.
- Pin the pending ID, trusted state path and evidence fingerprint at review.
  Verify them again at decision time and require renewed review on changes.
  Default confirmation to Cancel. Require a nonempty reject reason. Invoke the
  trusted CLI with fixed argv and no shell, capture and redact outputs, then
  re-read state. The current check/write sequence needs shared serialization
  across decision writers, holding the same lock for the final recheck and decision,
  or a common atomic compare-and-decide primitive before
  enabling concurrent dashboard writes. Use existing resume commands separately;
  those validate the brief digest and consume approval once. A decision does not
  resume, merge or change gate policy.

## Phased delivery

Each row is scoped for one agent. Tests use temporary fixtures and simulated
processes/time without running real agents or contacting services.

| Phase | Task | Acceptance |
| --- | --- | --- |
| MVP | Contract and pack adapter | Assert broker/run reconciliation without duplicate rows, direct dispatch, early steps and blocking versus terminal checkpoints. |
| MVP | External adapter | Assert attempts and event pairing, missing result, reused PID, unknown quota and colliding native IDs. |
| MVP | Incremental reader and store | Assert partial UTF-8/JSONL, replacement, truncation, oversize records and stale metadata. Measure bounded reads and UI response under fixture load. |
| MVP | CLI and curses navigation | Assert schema and exit codes, command/message filters, no writes in read mode and dependency-free fallback. Walk both viewport sizes and resize over SSH/tmux. |
| MVP | Operator decisions | Assert allowed roles and CI path, hashed identity, sandbox refusal, rejection note, stale confirmation and one decision across concurrent supported writers. Enable only with protected host state and shared writer safety. |
| V1 | Accounting and incidents | Assert local-day boundaries, missing versus zero, no double counting, aged pool data and human confirmation precedence. |
| V2 | Optional renderer and notifications | Assert renderer parity, fallback and periodic reconciliation after dropped notifications. |
| V2 | Explicit cross-backend goal links | Assert verified joins, retry edges and standalone views when producer IDs are absent. |

## Risks and open questions

- Confirm stable external goal, attempt and gate IDs and local-only pool export.
  Coverage is partial until producers expose those fields.
- Confirm producer host/start metadata for PID checking. Tune quiet thresholds
  against buffered output, long commands and mounted filesystems.
- Validate accounting provenance for default zeros and aggregate-only runs.
  Record coverage before treating totals as spend.
- Validate operator isolation and shared decision writer safety before rollout.
  The existing separate existence check and write permit competing writers.
