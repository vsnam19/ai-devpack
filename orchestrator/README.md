# Orchestrator Configuration (`orchestrator/`)

Drop-in Claude Code project configuration for a Sonnet orchestrator session that decomposes tasks, writes closed worker briefs under `briefs/`, dispatches workers, and verifies deterministic gate outputs.

## 1. Installing into a project

1. Copy the orchestrator files into the target repository root:
   - `orchestrator/CLAUDE.md` -> `CLAUDE.md`
   - `orchestrator/.claude/settings.json` -> `.claude/settings.json`
   - `orchestrator/.claude/agents/*` -> `.claude/agents/`
   - `orchestrator/briefs/` -> `briefs/`
2. Copy `.devpack/project.example.toml` to `.devpack/project.toml` and set `data_class` (`public`, `internal`, `confidential`, `customer-restricted`, or `secret`), `engines_config`, and `runs_root`.
3. Configure `engines.toml` (starting from `runner/engines.example.toml`) with your cloud and/or local on-prem engines. Ensure `runs/` is listed in `.gitignore`.
4. Note that `.claude/settings.json` permits `Read`, `Glob`, `Grep`, `Edit`/`Write` only in `briefs/**` and `runs/**`, and `Bash` only for `python3 runner/devpack_dispatch.py *`, `python3 runner/devpack_logs.py *`, and `git status|diff|log *`.

## 2. Three supported topologies

| Topology | Orchestrator | Workers | When to use |
|---|---|---|---|
| **1. Sonnet -> Haiku (native sub-agents)** | Claude Code (Sonnet) | `.claude/agents/implementer-haiku.md`, `reviewer-sonnet.md` | All-cloud `internal` / `public` repos where one API endpoint serves every agent. |
| **2. Sonnet -> Haiku / local (via `devpack_dispatch.py`)** | Claude Code (Sonnet) | `runner/devpack_dispatch.py --engine haiku\|local` | Hybrid or mixed-endpoint setups, or whenever deterministic gate/scope/test-lock/preflight-lint enforcement per worker job is required. **Required when mixing cloud and local engines**, because native sub-agents share ONE endpoint. |
| **3. Local alone (via `devpack_run.py`)** | `runner/devpack_run.py` | Local `class = "local"` engine(s) | `confidential` or air-gapped repos where cloud models are disallowed by `.devpack/project.toml` (`policy/data_governance.md`, `policy/local_models.md`). |

## 3. Sandbox and isolation policy

Consistent with `policy/sandbox.md`, agents run **sandboxed by default**:

- **Container default (`--isolation docker`).** `runner/devpack_run.py`, `runner/devpack_dispatch.py`, `eval/run_eval.py`, and `skilltest/skilltest.py` all default to `docker` isolation, mounting only the task working copy and read-only per-call credentials (for cloud engines), with no host home directory or host sockets.
- **Unsandboxed local mode (`--isolation local`) is refused by default.** Running `isolation = "local"` exposes the host filesystem and network to the agent's `Bash` tool, which is a deviation under `policy/deviations.md`.
- **Explicit opt-in required.** To run with `--isolation local`, you must pass `--allow-unsandboxed-local` on the CLI or set `allow_unsandboxed_local = true` in `.devpack/project.toml`. Every authorized local-isolation run prints a warning to stderr and records `sandbox_warning` in `run.json`.
- **Repository content is data, not instructions.** Briefs and orchestrator prompts treat source files, logs, and issue text strictly as untrusted data.

## 4. Reading logs as a human (`devpack_logs.py`)

Every dispatched job and runner workflow writes a redacted v0.2 run directory under `<runs_root>/<run-id>/` (`run.json`, `steps/`, `transcript.md`, `index.html`).

Generate a browsable static HTML index across all runs:

```bash
python3 runner/devpack_logs.py site runs
```

Open `runs/index.html` in any browser (zero JavaScript, zero external assets). For terminal inspection:

```bash
python3 runner/devpack_logs.py list runs
python3 runner/devpack_logs.py show runs/<run-id> --step 01
python3 runner/devpack_logs.py render runs/<run-id>
```
