# CLI dashboard for pipeline and agent observability

The dashboard reads local runner artifacts to answer four questions. Which jobs
are active or waiting? What failed? What evidence supports an approval? Which
measurements are available? The MVP provides Jobs, Job Detail, Events, Gates
and Approvals in curses, plain text and JSON. It can read older runs before any
producer changes ship.

## Views and navigation

Default to Jobs. Use one pane with drill-down at 80×24 and a list with a detail
pane at 160×48. Below 80 columns hide secondary columns and wrap details.
The header shows source health, refresh age and the pending approval count.
Preserve selection by stable key across refresh and resize.

- Use arrows or `j/k` to move, Page Up/Down to scroll, Tab to change focus,
  Enter in a list to open the selection, Esc to go back, `?` for help, `r` to
  refresh and `q` to quit.
- Use `/` to edit an AND filter on backend, state, role, agent, model or literal
  text. Enter in the editor applies the filter and returns to the list. Esc
  cancels the edit and keeps the applied filter. `C` clears it from the list.
- Job Detail shows observed attempts and steps, agent/model, outcome, gate
  state, times, process evidence, diagnostics and usage where available.
  Use `l` for Events, `g` for Gates and `e` for bounded diagnostic excerpts.
  Press `a` from Jobs or Job Detail to open Approvals, including when no job
  is selected. Esc returns to the originating view.
- Events uses `c` to cycle all, tool and message, and `f` to toggle
  follow. Scrolling back pauses follow. Shell tool calls and results appear
  under tool with bounded redacted input/output and any error flag. Per-command
  exit codes are unavailable for these records. The all filter can tail plain
  stdout, while step stderr becomes available in Diagnostics after completion.
- Gates shows receipt, attempt or step number, status and the applicable
  redacted output. A failed command and a failed gate are distinct observations.
- Approvals lists pending and decided records. Enter opens Evidence with
  summary, gate detail, brief lint findings and bounded redacted brief,
  escalation or memory proposal text where safely linked. Show hard-stop and
  blocking state when supported. Display an operator command for pending records.
- V1 adds Overview, calendar-day accounting and Incidents. V2 adds Pipeline per
  goal with explicitly linked jobs, dependency arrows and retry branches.
  Unlinked jobs keep a flat timeline. Label expected stages and show skips
  only when supported by workflow semantics or producer evidence.

```text
Jobs   Pack current | 1 pending approval
Job      Backend   State                 Agent/model
R17      pack      waiting for approval  worker-a/model-1
R19      pack      running               worker-a/model-1

Detail R19  Process health unknown  No stream output yet
/ Filter  C Clear  Enter Detail  l Events  g Gates  e Diagnostics  a Approvals
```

## Snapshot contract

Use a backend, configured source and native ID for each logical job, plus a child
ID for each observed step or attempt. A broker request keeps its request key when
the worker link arrives. Preserve run and attempt references as aliases and
retarget any open selection during reconciliation. Keep native status and source
references alongside normalized state. Use null in JSON and “unknown” in the UI
when evidence is missing. Track observation time, last event time and process
health independently. File modification time is an activity hint.

Normalize verified outcomes before considering stream or PID hints. A paused
execution becomes ready to resume only with a linked approved checkpoint and
eligible continuation. Keep terminal checkpoints alongside the execution outcome.
A pre-merge checkpoint can remain pending after a successful execution. Preserve
raw outcomes and expose conflicting receipts or missing links as source notices.

### Pack mappings

Each source below names the current producer or the relevant reader where the
dashboard reuses field interpretation. Reuse field semantics with bounded
readers. Values labeled as future appear in Phase 0.

| Artifact | Fields and interpretation | Code |
| --- | --- | --- |
| `<runs-root>/<run-id>/run.json` | `run_id` (directory fallback), `kind`, `task` (task label), `started`, `finished`, `outcome`, `engines` (configured aliases, list or role mapping). Derive final wall duration from valid start/end times for terminal outcomes. A pause can populate `finished`; use normalized state to interpret it and include pause time in end-to-end wall duration. | `runner/agent_exec.py:new_run_dir`, `runner/agent_exec.py:update_run_json`, `runner/devpack_logs.py:load_run_dir` |
| Task `run.json` | `approval_id` and `checkpoint_id` link a brief pause. Completion adds `checkpoints[]` with `approval_id`, `checkpoint` and `status`. `git_metadata_tampered`, `scope_violation`, `tamper_reason` and any `refusal_reason` explain early failures or refusals. | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_run.py:_resume_from_approval` |
| Dispatch `run.json` | `approval_id`, `gate` (ok/fail/skipped where written), `scope`, `brief_lint`, `refusal_reason`, `memory_ids`, `memory_hashes`, `memory_tokens`, `memory_proposals` when present. | `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/devpack_dispatch.py:_resume_dispatch_from_approval` |
| `run_summary.json` | Task `status`, `verdict`, `escalation_reason`, `step_logs`, `memory_ids`, `memory_tokens`, `memory_proposals`, `cost_usd`, `tokens_in/out` and `duration_s`. Duration is accumulated invocation time. Legacy totals use the accounting rules below. The summary can precede or lag the corresponding run state on resume. Isolation refusal can leave no summary. Rejected resume can replace only the log-directory copy with decision fields, leaving a stale run-directory copy. For refusal and rejection use `run.json` for state and reason when present, and use run-local step artifacts when summary fields are absent or stale. | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_run.py:_resume_from_approval` |
| `steps/NN-<role>-<engine>.meta.json` | Step number and fallback role/engine from stem. Read `role`, `engine`, `model` (may fall back to configured model), `init_model` (observed where present), `status`, `exit_code`, `duration_s` or legacy `duration_ms`, `error`, `stderr`, `final_text` and memory IDs/hashes/tokens. | `runner/agent_exec.py:run_agent` (includes `_write_meta_and_return`), `runner/agent_exec.py:AgentResult.to_dict`, `runner/devpack_logs.py:_parse_step_stem`, `runner/devpack_logs.py:_apply_step_meta` |
| Same step metadata | Input aliases `tokens_in/input_tokens/in_tokens`, output aliases `tokens_out/output_tokens/out_tokens`, legacy nested `tokens.in/out` when the legacy flat value is absent or defaulted to zero, and `cost_usd/total_cost_usd/cost`. Preserve explicit values only with the applicable provenance below. | `runner/devpack_logs.py:_apply_step_meta`, `runner/agent_exec.py:parse_stream`, `runner/agent_exec.py:run_agent` |
| Operator-selected `engines.toml` | Resolve an identified step engine alias to its configured model, labeled configured rather than observed. Match the runner's configuration when available. | `runner/agent_exec.py:load_engines`, `runner/devpack_run.py:load_engine_config` |
| Matching `.jsonl` and `.prompt.md` | Discover a step before its metadata appears. Use recognized stream variants for tool/message kind, phase, call ID, text and error flag when present. Pair IDs within the step and keep byte order. Use bounded prompt excerpts on request. | `runner/agent_exec.py:run_agent`, `runner/devpack_logs.py:load_run_dir`, `runner/devpack_logs.py:parse_stream_jsonl` |
| `step_NN_gate.json` | Task gate by step index and round. `gate_passed` is authoritative for the attempt. Show `step_index`, `round`, `returncode`, `command`, `duration_s`, `timestamp`, `lint_findings`, `lint_clean`, `static_findings`, `static_clean`, `stdout` and `stderr`. Read under the run directory or an explicitly configured, verified log directory. `--log-dir` can put numbered JSON logs elsewhere even when the summary is copied to the run directory. A basename in `step_logs` alone cannot identify that directory. | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_run.py:write_step_log`, `runner/devpack_run.py:main` |
| `steps/02-gate-receipt.json` | Dispatch trusted gate config path may produce this receipt. Show `gate_status`, `first_failed_check`, `timestamp_utc`, `checks` and `error`. Use it for the gate invocation and the dispatch `run.json.gate` for the overall dispatch gate decision. | `runner/devpack_dispatch.py:_resolve_gate_command`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `enforcement/gate/gate.py:write_receipt` |
| `steps/NN-gate.txt`, `transcript.md` | Gate text is evidence. Without a structured status, accept an unambiguous standalone gate marker from a known marker-producing gate. Otherwise show gate unknown. Task gate commands can fail without any marker. Transcript is a generated snapshot. | `runner/devpack_run.py:write_gate_step_file`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/devpack_logs.py:load_run_dir`, `runner/devpack_logs.py:write_rendered_outputs` |
| `.devpack/queue/<id>.{request,response}.json`, wrapper copies | Queue filename identifies request. Wrapper has `task=broker-request` and the copied request. The basename of redacted response `run_dir` may identify the worker under the configured root. Use worker outcome as execution state. Show response `status`, `error`, `changed_files`, `served_model_id`, `cost_usd`, `tokens_in`, `tokens_out` and `duration_s` where present as corroborating evidence. Response status may describe the agent invocation and cannot establish workflow success. Keep wrapper outcome as broker disposition and use its explicit status and approval ID for wrapper-only requests. | `runner/devpack_broker.py:Broker.poll`, `runner/devpack_broker.py:Broker.execute`, `runner/devpack_broker.py:Broker._execute_sandboxed_worker`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg` |
| `<state_dir>/approvals/{pending,decided}/<id>.json` | `id`, `checkpoint`, `kind`, `hard_stop`, `status`, `subject`, `created`, `decided_at`, `decided_by`, `summary`, `note`, `gate_detail`, `gate_receipts`, `evidence`, `requested_by`, redacted `run_dir`, `consumed` and allowlisted `metadata`. Prefer valid decided record during transition. Apply the approval joins below. Blocking status comes from an explicit paused execution or checkpoint semantics. | `runner/checkpoints.py:request`, `runner/checkpoints.py:decide_approval`, `runner/checkpoints.py:list_approvals`, `runner/checkpoints.py:consume_resume_approval` |
| `<state_dir>/runs/<id>.json`, `<id>.consumed` | Look up by approval ID. Require an approved record, matching supported continuation with a compatible `checkpoint_id` and workflow, and absence of `consumed` flags or marker. Report eligibility as observed, with actual validation and consumption left to the resume command. Withhold saved payload. | `runner/checkpoints.py:save_resume_state`, `runner/checkpoints.py:load_resume_state`, `runner/checkpoints.py:consume_resume_approval` |
| Linked evidence and memory proposal | Use linked run `brief_lint`, task `escalation_reason`, approval `summary` and `gate_detail`, and exact validated evidence files. Resolve memory proposal text only by a recorded ID under a trusted memory root. Skip missing, redacted-away or unsafe references. | `runner/checkpoints.py:request`, `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/memory.py:_ensure_memory_decision_checkpoint`, `runner/memory.py:find_memory_entry` |
| `attribution.json` (V1) | `suspected_cause`, `secondary`, `confidence`, `signals`, `evidence`, `analyzed_at`; latest valid `confirmed[]` with `cause`, `secondary`, `note`, `by`, `timestamp`. | `runner/devpack_logs.py:analyze_run_dir`, `runner/devpack_logs.py:attribute_run_dir` |

For an active step without metadata, pick the latest numbered prompt/stream
stem using `runner/devpack_logs.py:_parse_step_stem`. Treat an unmatched
prompt/stream as a step hint while execution is running. Label its role and
engine from the stem and model from the configured alias when available.
Configured task role mappings describe the run plan. Show observed model only
when evidence identifies it. `runner/agent_exec.py:run_agent` publishes
step metadata and buffered stderr after exit.

#### Outcome normalization

Apply these rules to Task and Dispatch/Broker. `run.json` takes precedence
over a stale summary. For a linked broker job show worker execution state and
broker disposition separately so a later broker failure remains visible.

| Native evidence | Dashboard state | Code |
| --- | --- | --- |
| `running` | Running, with independent health | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg` |
| `awaiting_approval` | Waiting for approval, or ready to resume with eligible approved continuation. A linked rejection displays rejected even before a resume attempt updates the run. | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/devpack_broker.py:Broker.execute`, `runner/checkpoints.py:decide_approval` |
| `rejected` | Rejected | `runner/devpack_run.py:_resume_from_approval`, `runner/devpack_dispatch.py:_resume_dispatch_from_approval`, `runner/devpack_broker.py:Broker.execute` |
| `refused`, or wrapper `response.status=refused` with wrapper `outcome=fail` | Refused, with available reason | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/devpack_broker.py:Broker.execute` |
| `fail`, `timeout` | Failed, with timeout reason where recorded | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/devpack_broker.py:Broker.execute` |
| `pass`, `APPROVED` | Succeeded. A pending pre-merge approval remains separately visible. | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`, `runner/devpack_broker.py:Broker.execute` |
| `ESCALATE_TO_HUMAN` | Failed with escalation reason, or waiting when a linked checkpoint explicitly paused execution | `runner/devpack_run.py:run_task_workflow` |
| `git_metadata_tampered` outcome, flag or `scope_violation` value | Failed with tampering reason, overriding a success verdict | `runner/devpack_run.py:run_task_workflow`, `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg` |
| Missing or unrecognized | Unknown | Adapter fallback |

#### Approval joins and evidence

1. Match exact approval IDs from top-level `run.json.approval_id` or
   `checkpoints[].approval_id`. Validate the checkpoint where recorded.
2. Use the basename of approval `run_dir` only as a candidate native ID
   within the configured source. `runner/checkpoints.py:request` redacts the
   path through `runner/redaction.py:redact_sensitive_text`. Never open it
   as a filesystem path. Require a unique discovered match and surface
   conflicting or redacted-away IDs as unresolved.
3. For memory decisions, match `requested_by=run:<run-id>` when present
   and corroborate with `memory_proposals.pending` when available. Match
   `metadata.memory_id` (or exact subject ID) against pending proposal IDs
   in Task summaries or Dispatch run metadata. See
   `runner/memory.py:_ensure_memory_decision_checkpoint`,
   `runner/devpack_run.py:run_task_workflow` and
   `runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`.
   Keep unresolved or standalone approvals visible and independent of
   execution blocking state.

Read `00-brief-lint.json` from the run root (broker) or `steps/` (Task
and Dispatch), as written by `runner/devpack_broker.py:Broker.execute`,
`runner/devpack_run.py:run_task_workflow` and
`runner/devpack_dispatch.py:_dispatch_job_with_trusted_cfg`. Read its `brief_lint` findings and reported `error_count`. Direct runs paused
at brief may have no prompt artifact yet. Use a linked step-01 prompt or, for
a broker pause, resolve the request's relative `brief` reference within the
trusted workdir and require the recorded `brief_sha256` to match before
showing a bounded redacted excerpt. The request stores a reference, as read
by `runner/devpack_broker.py:Broker._validate`, and the wrapper records the
digest in `runner/devpack_broker.py:Broker.execute`. If evidence is absent
or fails validation, show brief text unavailable. Use continuation files
only for allowlisted eligibility checks.

#### Broker reconciliation

Correlate queue request ID → wrapper copy of `<id>.request.json` → worker
directory identified by a validated Phase 0 correlation record or the basename of a wrapper or queue response's redacted `run_dir`. The wrapper
response can exist before the queue response. Check that the target is a
distinct direct child of the configured runs root, with a compatible run ID and
kind, and reject conflicting links. Treat legacy terminal response links as
valid only after those checks. Collapse the wrapper and worker into the request
row and retain both sources in detail. A refusal before worker creation remains
a single request row with no worker. A response naming the wrapper itself is
a wrapper-only outcome (for example, refusal or an approval pause). Direct
dispatch continues to appear by its own run ID. Ignore temporary request files.

Before Phase 0, a queued request without a matching wrapper has unconfirmed
processing status. A wrapper without a worker link shows “worker link unknown”.
Keep any observed worker as an independent run with broker membership unknown.
The same active job can therefore occupy two unresolved rows on legacy data.
Never infer links from start times, engine names or task names. A submit timeout
does not establish a job outcome.

### External adapter (V1)

The external runner has no producer function in this repository. These are
candidate fields for an operator-configured local file contract. Enable each
mapping only after validating it against local fixtures from the selected
producer. Pack stream interpretation is a reference for event variants only.

| Input beneath `<jobs-root>/<job-id>/` | Mapping and validation |
| --- | --- |
| Directory, `request.json`, `brief.md` | Directory ID; requested role/agent/model where the producer declares them. A bounded brief excerpt is available on request. Goal ID requires an explicit contract in V2. |
| `result.json` and attempt results where supported | `status`, `exit_code`, `agent`, `model`, `started`, `ended`, `duration_s`, `attempts`, `changed_files`, `verdict` where verified. Attempt result fields take precedence over filename hints. Without a result, final state is unknown. |
| `pid` and producer identity | Candidate PID. The producer contract must supply host ID, boot ID and PID namespace identity for local probing. Missing or mismatched identity leaves health unknown. |
| `attempts/<n>-<agent>-<model>/stdout` | Attempt number and filename identity hints where the naming contract is unambiguous. Recognized `thread.started` and `item.started/completed` variants yield lifecycle, command, tool or message events. Pair IDs within an attempt. Reference `runner/devpack_logs.py:parse_stream_jsonl` for Pack variants. |
| Attempt stderr, `reply.md`, `gate/<n>.log` | Bounded redacted diagnostics. Link gate to attempt only with a verified numbering contract. Gate status requires explicit structured outcome or a known marker contract. |
| Optional dated local pool snapshot (V1) | Use only explicitly documented quota, cooldown and sample time fields. Missing, stale and unverified sources report unknown current quota. |

A verified active attempt or local process identity can support running.
Usage, approvals and attribution remain unknown unless a validated source
supplies the fields. Cross-source links require explicit producer IDs.

## Phase 0 data prerequisites

Each row is a separate producer change with an offline acceptance test.
Ship existing-artifact reads independently. Legacy degradation applies
per run and per field, including in mixed-version roots.

| Producer change | Acceptance test | MVP when absent |
| --- | --- | --- |
| Write atomic per-run `heartbeat.json` from the host workflow or dispatch owner, starting at run creation. Record schema version, run ID, PID, host and boot IDs, PID namespace, process start identity, heartbeat time and nullable `last_event_at`. Refresh during waits for subprocesses, keep heartbeat and event times separate, and stop updates on pause or completion. Bind local checks to a trusted local source. | With fake time and processes, assert a quiet live child leaves the host heartbeat fresh, a killed owner leaves stale data, and boot mismatch, PID reuse and permission errors never verify liveness. Assert terminal and paused execution overrides health hints. | Process health unknown for Pack. Show available event time and elapsed age. |
| Publish the Task resume transition in `runner/devpack_run.py:_resume_from_approval` after successful consumption and before role execution. Set `outcome=running`, clear the pause's `finished` and preserve the original run ID and start time. Finalize subsequent failures with an explicit outcome. | Pause a stub role after resuming a previously approved task. Assert the original row moves to running with no finish time, a failed continuation gets a terminal outcome, and a second resume cannot consume the same approval. | A consumed continuation with legacy pause metadata shows resume status unknown until the next authoritative outcome. |
| Publish one atomic broker correlation record under the wrapper as soon as dispatch allocates the worker directory, before worker steps start. Include request ID, wrapper run ID and worker run ID. Retain response link as a completion fallback. Preserve an allocated link through errors and keep requests that allocate no worker distinguishable. | Pause a stub worker before step 01 and assert the queue, wrapper and worker produce one request row. Assert the same key after completion, refusal before allocation, failure after allocation and a concurrent direct dispatch. Reject conflicting or escaping links. | Use validated terminal response links. Show unresolved linkage during execution. |
| Preserve nullable measured cost through `runner/agent_exec.py:parse_stream`, `runner/agent_exec.py:run_agent` and task/dispatch serialization. Record schema or explicit provenance distinguishing measured values from defaults. Expose per-direction token availability too. | Parse fixtures with explicit zero, positive cost, missing result, truncated stream and local unpriced execution. Assert zero survives, absent values serialize as null, and a mixed run contributes known spend with partial coverage. Assert legacy consumers still handle nullable values. | Legacy default zeros are unknown in MVP. Versioned measured zero is displayed as zero. |
| Standardize run-level totals in each run directory's `run_summary.json`. Extend task summary with measurement scope and known/eligible counts. Emit the same summary for direct dispatch. Store cost, tokens and invocation duration with coverage, separately from wall duration. Publish the final snapshot atomically and refresh it coherently on resume. | Assert task retries are counted once, a direct dispatch has matching step and summary, early refusal has unknown measurements, and replay/re-read never adds totals twice. Assert external task log directories still leave the summary discoverable. | Read existing task summary and step metadata with legacy provenance rules. Use step fallback or unknown for missing totals. |
| Mirror task `step_NN_gate.json` into the run directory when an explicit log directory differs. Preserve redacted gate output and publish the structured result atomically for each attempt. | Use a failing stub gate with exit 1 and no marker, followed by a successful retry. Assert both verdicts and round links are visible from the run root with a separate log directory. | Use a verified log root if available, otherwise show the gate transcript with unknown status when structured evidence is missing. |

Legacy Task resume can reuse a directory while leaving its prior
`awaiting_approval` outcome until completion
(`runner/devpack_run.py:_resume_from_approval`,
`runner/devpack_run.py:run_task_workflow`). A consumed approval with a paused
run and no newer execution state shows “resume status unknown”. A new prompt,
stream or file mtime alone cannot verify that the resumed execution is active.

## Incremental reading and health

- Use stdlib polling with a bounded history window. Enumerate run IDs and
  prioritize selected and known active runs, then use parsed run ID timestamps
  and `run.json` and `steps/` activity hints to rank remaining candidates.
  Refresh activity hints for historical runs so older resumed jobs can
  re-enter the window. `runner/agent_exec.py:update_run_json` writes inside
  the run directory, `runner/agent_exec.py:run_agent` writes under `steps/`,
  and `runner/devpack_run.py:_resume_from_approval` reuses the run directory.
  The run directory's own mtime is insufficient.
- Expose the history limit and coverage for interactive Jobs. One-shot Jobs
  enumerates all IDs and evaluates metadata for all candidates before filtering
  and applying the output history limit. Report matched, returned and unreadable
  counts. Resolve show, gate and tail directly by validated run ID or broker
  request ID (through queue and wrapper lookup), outside the history window.
  V1 adds progressive historical hydration and paging at 10,000-run scale.
- Sample selected stream and approvals every 1s, known active metadata every
  3s and discovery every 5s. Back off to 30s when idle. Stop active polls for
  finalized, drained streams and wake readers on manual refresh.
- Parse stream records only for selected Events or explicit tail. Use a
  background polling worker and coalesce snapshots. Keep 1,000 selected events,
  records up to 256 KiB, and a 2 MiB read budget per background tick shared with
  evidence reads. Preserve the last good snapshot with age and error badges
  during partial writes. Distinguish empty, removed, inaccessible and stale
  sources.
- Track binary offsets and device/inode per generation. Buffer incomplete UTF-8
  and records. Mark a gap and reset offset on replacement or truncation.
  Count oversized or malformed complete records in recognized JSONL mode.
  A bounded valid final record without newline may be accepted after terminal
  state and two stable polls. For plain text, tail complete redacted lines.
- Classify streams as empty, recognized JSONL or plain text from bounded samples
  of complete records. An empty file shows “No stream output yet (buffered or
  non-streaming)” with elapsed age and available mtime. Plain text displays as
  activity and never contributes to JSONL error counts. Surface uncertain format
  explicitly. Use observed time when event timestamps are unavailable.
- With a known running job and a meaningful activity baseline, five minutes
  without activity can yield quiet. An empty stream alone provides no baseline.
  Heartbeat freshness contributes to health, independently of event silence.
  Use monotonic polling and UTC source timestamps with local display.
- For trusted local Pack heartbeats, match host, boot, PID namespace and process start identity before
  showing verified alive. On stale heartbeat and confirmed owner absence or
  confirmed PID reuse under the same boot, show suspected stuck/orphaned only
  while the execution is still nonterminal. For V1 External, probe only after matching producer host, boot and PID namespace.
  Local `ESRCH` supports suspected orphaned after a re-read of unchanged
  identity, attempt and PID and confirmation that no terminal result appeared. A PID-only process that exists remains unverified.
  Remote roots, permission denial and unverifiable identities leave health
  unknown. Exclude queued jobs, known waits and terminal receipts.
- In V1, target keyboard response below 100 ms on a local fixture with 10,000
  historical runs and 100 active jobs during scan and tail. Measure input
  latency, bytes read and backlog separately from time to full coverage.

## Usage provenance

Prefer a scoped authoritative run summary and count each retry once. If
coverage or scope is unavailable, use eligible step measurements. Never add a
run total to the steps it covers. Show token coverage separately from cost
coverage.

Legacy Pack cost defaults to zero in `runner/agent_exec.py:AgentResult`,
`runner/agent_exec.py:parse_stream` and task aggregation in
`runner/devpack_run.py:run_task_workflow`. Accept positive finite historical
cost where attributable to the invocation. MVP accepts zero only with
versioned measured provenance. Legacy zero and absent values are unknown,
including defaulted token counts. V1 may recover measured legacy zero from a
recognized result event (`total_cost_usd/cost_usd/cost`) during explicit
accounting, with bounded reads and validated provenance. Configured time and
context limits are capacity hints from `runner/agent_exec.py:load_engines`.

## Accounting and incidents (V1)

Use the local calendar day of completion. Distinguish known subtotal, fully
priced jobs, eligible jobs and incomplete historical coverage.

Read existing attribution directly with bounded metadata reads. Show latest
valid human confirmation alongside suspected cause, confidence, timestamp and
evidence. Display unknown when absent. Keep analysis and attribution writes in
their existing CLI. The dashboard never invokes `analyze_run_dir`, which also
loads entire streams and can scan the repository. Incidents show bounded
redacted gate, stderr, error, refusal reason or reply excerpts.

## Runtime, CLI and configuration

Use Python 3.10+ stdlib with curses differential painting and terminal restoration.
Use plain text for pipes, missing curses, unsupported terminfo or `TERM=dumb`.
Use the same redacted snapshot contract for all views.

```text
devpack_dash.py [--workdir PATH] [--dashboard-config PATH]
                [--project-config PATH] [--runs-root PATH]
                [--state-dir PATH] [--plain|--json] [--once]
                [jobs [--filter QUERY] | show JOB | gate JOB
                 | tail JOB [--kind all|tool|message]
                 | approvals [--status pending|decided|all]]
```

Plain and JSON return one snapshot except for explicit continuous tail.
`--once` uses plain by default and returns a snapshot or initial tail page. V1 adds backend selection and `--jobs-root`. JSON includes schema version, observation time, source health, nullable
fields and scan completeness. Streaming JSON uses one record per event or
notice. Require backend qualification for ambiguous IDs. Exit 0 for successful
reads, 1 for read failure or all selected roots unavailable, 2 for invalid
arguments or config. A failed job is data in a successful read.

Resolve workdir and project config the same way as
`runner/devpack_approve.py:_resolve_state_for_cli`. Resolve approvals from
explicit `--state-dir` via `runner/checkpoints.py:resolve_state_dir` or from
trusted host pipeline settings via `runner/checkpoints.py:load_pipeline_config`.
A dashboard display config cannot redirect approval state. For a broker with
external storage, pass the host broker's `--runs-root` to the dashboard and
resolve the same trusted checkpoint state as the broker. Keep the queue root
relative to the selected project workdir. See `runner/devpack_broker.py:Broker.__init__`.

Read `[dashboard]` only from an explicitly selected operator-controlled
`--dashboard-config` outside worker-writable trees. Accept runs root, verified
task log/evidence roots, poll intervals, quiet threshold and history limit.
V1 adds external roots. CLI overrides config. Resolve relative root paths
from workdir, matching the runner's project-relative convention
(`runner/agent_exec.py:load_project_config`,
`runner/devpack_dispatch.py:dispatch_job`). Project config supplies
project context through the trusted runner resolver. It cannot supply dashboard
roots or authorize evidence locations. Default to Pack, workdir-relative
`runs/` and normal trusted pipeline state resolution.
For standalone task runs, point the dashboard at the task runner’s
`--runs-root` (its default is `/tmp/devpack-run/runs`), or configure a parent
containing the standalone `--log-dir` run. See `runner/devpack_run.py:main`.
Validate poll intervals and quiet threshold as positive integer seconds and
reject fractional or string values to support the Python 3.10 fallbacks.
See `runner/agent_exec.py:_minimal_toml_loads` and
`runner/workspace_scope.py:parse_toml_with_arrays`.

## Read boundaries and decisions

- Read bounded regular files under configured roots. Restrict recognized
  filenames and reject escapes and symlinks using directory-relative no-follow
  opens where supported. Treat payload paths and content as untrusted.
  The MVP performs no writes, network requests, signal delivery or execution
  of commands found in artifacts.
- Strip ANSI escape sequences and terminal controls, including split escape
  sequences across input chunks. Allowlist fields and reuse one
  `runner/redaction.py:Redactor` instance per reader configuration. Redact
  complete logical records before excerpting. If oversized or incomplete
  records cannot be redacted safely, show metadata only. Apply to terminal,
  plain, JSON, diagnostics and filters. Keep raw text out of snapshots and caches.
- Evidence is visible only through bounded excerpts under these rules. Include
  diagnostics for failures before the first step. Constrain any brief,
  escalation or memory evidence path to trusted roots and exact recorded links.
  Follow the sanitization rules in [the logging policy](../../policy/logging.md).
- MVP displays shell-quoted commands for the operator to run separately.
  Build them from the trusted CLI path, resolved target workdir and state,
  selected config if explicit, and validated approval ID. A note is required
  for rejection. If required paths cannot be shown safely, mark the command
  unavailable. With all overrides set, the command forms are:

```text
python3 /trusted/runner/devpack_approve.py --workdir /target --project-config /trusted/project.toml --state-dir /trusted/state approve APPROVAL_ID
python3 /trusted/runner/devpack_approve.py --workdir /target --project-config /trusted/project.toml --state-dir /trusted/state reject APPROVAL_ID --note 'Reason for rejection'
```

V1 may enable dashboard decisions after every supported writer uses shared
serialization in `runner/checkpoints.py:decide_approval` and the trusted
`runner/devpack_approve.py` CLI accepts an expected evidence fingerprint.
Compute it over the reviewed approval and evidence digests. Under the shared
lock, re-read and compare the current pending ID, state and fingerprint before
deciding. Require renewed review on mismatch. Default confirmation to Cancel,
require a nonempty rejection reason, invoke fixed argv without a shell, redact
the result and reload approval state. Use protected operator state outside agent
mounts and host filesystem permissions. Keep hashed identity and provenance
notes through the existing CLI, using the target workdir. Resume through the
existing separate command, which verifies the brief digest and consumes approval
once.

## Delivery and acceptance

Use temporary offline fixtures, simulated process state and fake time.

| Phase | Deliverable | Acceptance |
| --- | --- | --- |
| Phase 0 | Separate producer changes above | Run each producer acceptance test independently and verify mixed-version fixtures. |
| MVP | Pack reader | Assert linked and unresolved broker runs, refusals before step 01, direct runs, a non-marker gate failure, alternate log location, plain-text and buffered output, missing results, reused PID, unknown fields and conflicting broker links. |
| MVP | Bounded store and renderers | Assert split UTF-8/JSONL and escape sequences, truncation, replacement, stale snapshots, selected-stream-only parsing and bounded reads. Assert an older resumed run re-enters the window, an older failed run appears in filtered one-shot Jobs, and detail commands resolve IDs outside the window. |
| MVP | Navigation and read-only Approvals | Assert `a` opens Approvals from both views without a selected job, redacted-path and memory approval joins, pause-to-ready eligibility, filter apply/cancel/clear, tool filtering, redacted evidence, exact command arguments for a target outside the current directory, no writes or subprocess launches, schema and exit codes, and both viewport sizes. |
| V1 | External adapter and progressive history | Assert configured fixtures, remote or unknown identity skipping PID probes, matching local identity with stale attempts, colliding IDs, paging coverage and the input latency target. |
| V1 | Overview, accounting and incidents | Assert local-day boundaries, unknown versus measured zero, retry deduplication, partial scan coverage, aged quota data, attribution confirmation precedence and diagnostics without an analyzer run. |
| V1 | Protected decision execution | Assert stale evidence/fingerprint refusals, one decision across competing supported writers, refusal from agent context, reject note enforcement and provenance in the target repository. |
| V2 | Pipeline per goal | Assert explicit cross-backend joins, observed retry edges and standalone views for missing goal IDs. |


## Appendix: open issues from design review round 3

These findings were not yet resolved when the design was submitted for approval. Each must be resolved in the phase that first depends on it.

- high — docs/design/dashboard.md:201 (also `:111`, `:207-212`) — Phase 0 and outcome normalization address Task resume reusing its run directory, but miss that Direct Dispatch and Broker resumes allocate a new run directory with `approval_id=""` and never update or link the original paused run. — Concrete failure scenario: In default `hitl` mode, an operator approves a paused `devpack_dispatch.py` or broker `brief` checkpoint and resumes it; the original paused run permanently keeps `outcome="awaiting_approval"` with a consumed approval and displays `"resume status unknown"`, while the actual resumed run appears as an unrelated second row with no link to the approval or original job. Fix: extend Phase 0 to record `resume_approval_id` / `paused_run_id` in the resumed Dispatch/Broker `run.json` and update the original paused `run.json` on approval consumption, and reconcile legacy resumes via `<id>.request.json` (`{"resume": aid}`) and approval `run_dir` links. — Evidence: read `runner/devpack_run.py:1965-2046`, `runner/devpack_dispatch.py:1166, 1317-1392, 1787, 2184-2227`, and `runner/devpack_broker.py:425, 516-530, 647`.

- medium — docs/design/dashboard.md:94-98 — Relying on `runner/devpack_logs.py:_parse_step_stem` for active steps without `.meta.json` misparses hyphenated roles such as `broker-worker`. — Concrete failure scenario: Every broker-dispatched worker runs with `role="broker-worker"` (`01-broker-worker-<engine>`), so `_parse_step_stem` (`split("-", 1)`) parses `role="broker"` and `engine="worker-<engine>"`; the `engines.toml` lookup for `"worker-<engine>"` fails, leaving `model` as `unknown` for the entire active step and causing `/` filters on `role` or `model` to miss running broker workers. Fix: match known engine names from `run.json.engines` or `engines.toml` from the right side of the stem (or handle known multi-word roles like `broker-worker`) instead of `split("-", 1)`. — Evidence: read `runner/devpack_broker.py:380`, `runner/devpack_dispatch.py:1399`, `runner/devpack_logs.py:561-573`, and `docs/design/dashboard.md:94-98`.
  **Resolved in MVP with right-side matching of configured engine aliases.**

- medium — docs/design/dashboard.md:143-150 (also `:90`) — Withholding `<state_dir>/runs/<id>.json` and only reading `brief` via step-01 prompts or broker requests leaves brief text unavailable for all Task and Direct Dispatch `brief` approvals in default `hitl` mode. — Concrete failure scenario: Because `devpack_run.py` and `devpack_dispatch.py` pause at `brief` before calling `run_agent()`, no `steps/01-*.prompt.md` exists yet and neither runner writes the brief text into `run_dir`, so opening Approvals (`a`) to review a pending `brief` checkpoint shows `"brief text unavailable"`. Fix: either emit a redacted `00-brief.md` (with `brief_sha256`) into `run_dir` on pause in Phase 0, or allowlist reading verified `brief_path` / `task_file` / `context` fields from the continuation record under SHA-256 and trusted-root checks while continuing to withhold `credentials_path`. — Evidence: read `policy/operating_modes.md:9-10, 24-28`, `runner/devpack_run.py:944-1031`, `runner/devpack_dispatch.py:1318-1392`, and `runner/checkpoints.py:1252-1257`.
  **MVP reports unavailable for an unverified brief. Publishing additional brief evidence remains open.**

- medium — docs/design/dashboard.md:216-235 — Sweeping `run.json` and `steps/` mtimes across all historical run directories every 5 seconds on a single background polling worker does not scale to thousands of runs or remote filesystems. — Concrete failure scenario: With 5,000–10,000 completed runs (especially on network/remote mounts), 20,000+ `stat()` calls every 5 seconds on the single background worker exceed the 1s/3s polling intervals, starving selected-stream tailing and active-job updates as well as stalling cold one-shot `jobs` invocations (`:224-226`). Fix: bound historical re-scans per tick (prioritizing new lexicographic IDs, runs referenced by active approvals/queue items, and round-robin slices of older directories) or run historical re-statting on a slower cadence separate from the 1s tail loop. — Evidence: read `docs/design/dashboard.md:216-235, 259-263`.
  **Resolved in MVP with root change detection and bounded historical rechecks.**

- medium — docs/design/dashboard.md:235 (also `:352-354`) — Applying the 256 KiB record limit and "show metadata only" rule uniformly to non-stream JSON files can discard authoritative gate and step verdicts. — Concrete failure scenario: `runner/agent_exec.py` writes untruncated `final_text` and `stderr` into `steps/NN-*.meta.json`, and `runner/devpack_run.py` writes untruncated `stdout` and `stderr` into `step_NN_gate.json`; when a failing C++ build or test gate produces >256 KiB of output, rejecting the oversized JSON file drops `gate_passed`, `returncode`, `status`, and `cost_usd` to `unknown` right when failure details are needed most. Fix: restrict the 256 KiB record cap to `.jsonl` stream records, and parse scalar verdict/usage fields from `.meta.json` and `step_NN_gate.json` while capping/redacting only large text fields (`stdout`, `stderr`, `final_text`) to bounded excerpts. — Evidence: read `runner/agent_exec.py:2508-2527`, `runner/devpack_run.py:1357-1377`, and `docs/design/dashboard.md:234-242, 352-354`.
  **Resolved in MVP with a separate structured JSON size limit.**

- low — docs/design/dashboard.md:85 (also `:93, :250`) — Multi-check Task/Dispatch gates write no artifacts until all checks finish, and `step_NN_gate.json` omits `preflight_lint`, `cppcheck`, and `scope_check` errors. — Concrete failure scenario: While `gate_cmd`, `preflight_lint`, and `cppcheck` run for >5 minutes after step 01 exits, no step 02 file exists so `Job Detail` appears idle/quiet; if `gate_cmd` exits 0 while `cppcheck` fails, `step_NN_gate.json` shows `gate_passed=false` with `returncode=0` and passing `stdout` while the error text is only in `steps/NN-gate.txt`. Fix: document that `Gates`/`Diagnostics` must pair `step_NN_gate.json` with `steps/NN-gate.txt` rather than relying on `step_NN_gate.json` `stdout`/`stderr` alone, and consider emitting `current_stage`/`step` in Phase 0 `heartbeat.json`. — Evidence: read `runner/devpack_run.py:1235-1377` and `runner/devpack_dispatch.py:1652-1747`.
  **MVP pairs the structured verdict with the gate transcript. Live stage evidence remains open.**

- low — docs/design/dashboard.md:200 (also `:253, :259`) — Requiring Linux PID namespace and boot ID matching before showing `verified alive` leaves local process health permanently `unknown` on macOS hosts. — Concrete failure scenario: Although the research target explicitly covers Linux and macOS workstations, macOS lacks `/proc/<pid>/ns/pid` and Linux `boot_id`, so every local Pack/External job fails identity verification and stays `Process health unknown`. Fix: gate PID-namespace matching on platforms that support PID namespaces and specify a Darwin boot-time/process-start fallback. — Evidence: read `.research/dashboard/report_db0_research.md:62-73, 117` and `docs/design/dashboard.md:200, 253, 259`.

- low — docs/design/dashboard.md:17-27 (also `:45-50, :366-369`) — MVP `curses` scope is slightly over-engineered in layout/query machinery while leaving `Jobs` columns, `/` filter syntax, and copy-safe approval commands underspecified. — Concrete failure scenario: Implementers spend MVP effort building synchronized 160×48 split-pane + 80×24 drill-down rendering and an AND-query filter without a defined `key=value`/quoting grammar or wide-mode column spec (the mockup omits task, health, duration, and cost), while ~140-character `devpack_approve.py` commands wrap inside an 80-column `curses` pane and break SSH/tmux copy-paste. Fix: defer the 160×48 side-by-side split pane to V1, specify exact `Jobs` columns and `/` filter syntax, and provide an unwrapped/borderless command view or `--plain` output for copying approval commands. — Evidence: read `docs/design/dashboard.md:17-27, 45-57, 366-369`.
  **Resolved in MVP with single-pane navigation, documented Jobs filters and unwrapped plain approval commands.**

- low — docs/design/dashboard.md:79 (also `:366-369`) — Minor omissions in the Dispatch `run.json` field list and blocking-checkpoint resume guidance. — Concrete failure scenario: Dispatch `run.json` writes `checkpoint` (not `checkpoint_id`), `git_metadata_tampered`, and `scope_violation`, and wrapper `run.json` writes `checkpoint`, none of which are listed in the Dispatch row at `:79`; additionally, operators approving a blocking `brief` checkpoint only see `approve`/`reject` commands and not the separate `--resume` command required to continue execution. Fix: list `checkpoint`, `git_metadata_tampered`, and `scope_violation` in `:79`, and show the follow-up `--resume` command hint for blocking checkpoints. — Evidence: read `runner/devpack_dispatch.py:1371, 1469-1470`, `runner/devpack_broker.py:462, 621`, and `policy/operating_modes.md:29-33`.
