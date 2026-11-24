# Orchestrator configuration

The Sonnet orchestrator decomposes a goal, writes briefs, submits jobs, and reads results. A separate host broker authenticates workers and runs the installed dispatcher. Only a standalone queue client is installed in the project; no host-executed pack code comes from the worker workspace.

Use installation topology A (external pack) from the root `README.md`. Vendored
`devpack/` is supported only for CI-only adoption and cannot be used for this broker
topology. Copy engine and project gate templates into trusted project configuration;
set `engines_config` and `gate_config` relative to `.devpack/project.toml`.

## Developer setup

Keep the pack installation, engine configuration, worker credentials, and run directory outside the target project. Use a trusted engine configuration based on `runner/engines.example.toml`. Set the project's `data_class` in `.devpack/project.toml` before starting the broker; the broker snapshots this policy at startup. Restart it after changing policy or engines.

For an older project installation, remove its legacy `.devpack/pack/` copy before starting; the broker refuses leftover executable pack files.

Copy `orchestrator/CLAUDE.md` to the project root and `orchestrator/.claude/` to the project's `.claude/` directory. Create `briefs/`. The settings allow Read/Glob/Grep, brief writes, Git inspection, and Bash for the standalone client only.

Start the broker in a second terminal, using your actual installation and project locations:

```bash
python3 /opt/devpack/runner/devpack_broker.py \
  --workdir /tmp/project \
  --runs-root /tmp/project-runs \
  --engines /opt/devpack/runner/engines.example.toml \
  --workers haiku,local \
  --credentials /tmp/worker-auth.json \
  --gate-cmd 'bash build_and_test.sh'
```

Worker credentials can also be supplied through `DEVPACK_CLAUDE_CREDENTIALS` in this terminal. Set `DEVPACK_LOCAL_BASE_URL` and `DEVPACK_LOCAL_AUTH_TOKEN` here when using the local engine. Keep these values out of the orchestrator environment. The broker installs `.devpack/pack/runner/devpack_dispatch.py` as a credential-free client, publishes `.devpack/broker.json`, and serializes jobs from `.devpack/queue/`.

Then start the Sonnet session in the project using its own isolated Claude configuration. Do not export worker credential variables into this terminal. Write a brief and submit:

```bash
python3 .devpack/pack/runner/devpack_dispatch.py --submit \
  --engine haiku --brief briefs/job.md --workdir . --allow 'src/**'
```

The installed client always submits. The full installed dispatcher also submits when `--submit`, `DEVPACK_BROKER=1`, or a project broker marker selects broker mode. `--timeout` bounds the client's wait (default 900 seconds); a client timeout does not cancel an active worker.

Requests contain an engine, a relative brief path, and nonempty allow globs. The broker refuses unknown fields, unknown or disabled engines, data-class violations, brief traversal or symlink escapes, and a modified client (SHA-256 compared with its own installed copy). An optional legacy `--gate-cmd` must exactly match the gate fixed at broker startup. Requests cannot change isolation, credentials, engine configuration, or executables. Restarting requires the broker singleton lock; concurrent jobs are refused and queue processing is serial with a polling interval of 100 ms.

Stop the broker with Ctrl-C after the session. Eval starts and stops one broker per orchestrator trial automatically. Eval uses `true` as its fixed per-job gate command in addition to dispatch's test-lock and lint checks; the final goal checker runs separately against the product tree.

## Pipeline modes and checkpoints

The broker snapshots `[pipeline]` (`mode = "hitl" | "autonomous"`, default `hitl`) from the host `.devpack/project.toml` at startup (`policy/operating_modes.md`). Before launching a worker, the broker enforces the blocking `brief` checkpoint against `<state_dir>` (default `$XDG_STATE_HOME/devpack/<project-hash>`, always outside the worktree and worker sandbox): in `autonomous` mode, `brief` auto-approves when `runner/brief_lint.py` passes with zero error-severity findings; in `hitl` mode, it records a pending approval, persists continuation state, and exits `20` (`AWAITING_APPROVAL`). Once a human approves via `runner/devpack_approve.py approve <approval-id>`, resume with `python3 .devpack/pack/runner/devpack_dispatch.py --submit --resume <approval-id>` (or `runner/devpack_broker.py --resume <approval-id>`); resuming checks that the brief SHA-256 is unchanged and consumes the approval exactly once. `runner/devpack_approve.py` is a human/CI-only command; the broker refuses any queue request that attempts to invoke or forward `devpack_approve`.

## Worker isolation and scope

Workers default to Docker. They receive a separate product copy without `.devpack/`, orchestrator settings, or host Git metadata. Their generated Git baseline is mounted read-only. Ignored `.devpack` writes are independently fingerprinted and always fail worker scope checks. Only regular product files from jobs with valid scope are copied back. Requests, refusals, and responses are logged under the host run root, alongside worker transcripts.

The orchestrator trial uses `--isolation local --allow-unsandboxed-local` and passes its scoped tool permissions to Claude. This local session is a permission boundary, not an OS filesystem sandbox. Authorized local workers (`--isolation local --allow-unsandboxed-local` on the broker) likewise have host filesystem access; use Docker for worker filesystem isolation. Worker secrets are never forwarded into the orchestrator environment.

Do not add `.devpack/` to worker scope exclusions. Eval grades a product-only copy without host-generated queue artifacts, while workers have an independent baseline and explicit control-file enforcement. Repository content remains untrusted data; stop and escalate after two fix rounds.

## Reading results

Read the client's single compact JSON response first: `status`, `gate`, `scope`, `changed_files`, and `final_text_tail`. Open `<run_dir>/transcript.md` only when needed. Responses are also available at `.devpack/queue/<id>.response.json`. The client returns 0 only when status, gate, and scope are all `ok`; failures and timeouts return 1, invalid arguments return 2.

Run log tools from the trusted installation in a developer terminal:

```bash
python3 /opt/devpack/runner/devpack_logs.py site /tmp/project-runs
python3 /opt/devpack/runner/devpack_logs.py show /tmp/project-runs/RUN_ID
```

Native sub-agents share one endpoint. Governed worker jobs and mixed cloud/local jobs must use the broker. Confidential projects require an engine whose declared data-class limit permits the project; secret projects are refused.

The executor selects tools by role: implementers and fixers retain Read/Edit/Write/Bash/Glob/Grep, reviewers receive Read/Glob/Grep, and orchestrators receive Read/Glob/Grep plus Write/Edit limited to `briefs/**` and the four Bash rules in `.claude/settings.json`. Every normal Claude launch passes `--permission-mode dontAsk`, so tools requiring approval are denied without prompting in print mode. Trusted engine configuration may override `allowed_tools` and `disallowed_tools` in `[roles.<role>]` alongside `engine = "<engine-name>"`; string role mappings retain the defaults. The orchestrator's submit rule uses Claude's `:*` argument-prefix syntax and requires `--submit` immediately after the installed client path. Eval independently audits every Bash tool call, reports forbidden commands in `disallowed_bash_commands`, and fails compliance for any command outside this allowlist, including shell composition, redirects, and substitutions. Briefs are read once through non-symlink directory descriptors before copying the product tree; copy-back pins directory descriptors and uses exclusive temporary files with atomic replacement or descriptor-relative deletion.
