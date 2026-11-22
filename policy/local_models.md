# Local model policy

On-prem and local models allow teams to process `confidential` repositories without
sending source code or requirement text to external cloud endpoints. Local engines are
configured in `runner/engines.example.toml` (`class = "local"`) and executed through
`runner/agent_exec.py` using an Anthropic-compatible endpoint.

## 1. When local AI is allowed (`policy/data_governance.md`)

| Repository `data_class` | `class = "local"` engine | `class = "cloud"` engine |
|---|---|---|
| `public` | Allowed | Allowed |
| `internal` | Allowed | Allowed |
| `confidential` | Allowed (on-prem, no external egress) | Refused unless `max_data_class >= "confidential"` |
| `customer-restricted` | Refused unless customer contract permits | Refused |
| `secret` | **Always refused** | **Always refused** |

- Data-class enforcement is fail-closed in `runner/devpack_dispatch.py` using
  `.devpack/project.toml` (`data_class = "internal"` when absent).
- Local engines must reference environment variable **names** (`base_url_env`,
  `auth_token_env`) in `engines.toml` — never literal URLs or tokens in the repository.
- `runner/agent_exec.py` exports `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, and `ANTHROPIC_DEFAULT_SONNET_MODEL`,
  unsets `ANTHROPIC_API_KEY`, and never copies cloud `.credentials.json`.

## 2. Server and protocol requirements

1. **Anthropic-compatible Messages API with native tool use.** The server must accept the
   Anthropic Messages API and emit structured `tool_use` content blocks rather than printing
   JSON tool calls as plain assistant text (`tool_protocol_errors`).
   - OpenAI-only servers (chat completions without Anthropic Messages support) require a
     translating gateway that preserves tool-use schemas and `tool_use` / `tool_result`
     blocks *(external claim — verify per server)*.
2. **Server-side context window >= 32,768 tokens (`min_context_tokens = 32768`).**
   Claude Code's system prompt, built-in tool schemas, and task brief exceed 4,096 tokens.
   Many local inference servers default to a 4k or 8k context window and silently truncate
   the system prompt, causing the model to emit JSON tool calls as plain text instead of
   `tool_use` blocks *(external claim — verify per server; observed in
   `runner/tests/fixtures/stream_local_tool_protocol_error.jsonl`)*.
3. **Latency budget and timeouts.** Configure `timeout_s` in `engines.toml` to match the
   measured hardware budget. Agent timeouts are engine capability/latency failures: `eval`
   records them in the `timeouts` column and counts them as **not passed** (never excluded
   as harness errors).
4. **Cost and telemetry reporting.** Claude Code may report synthetic `total_cost_usd`
   values for local endpoints using default cloud pricing. For `class = "local"` engines,
   `eval/run_eval.py` and `runner/devpack_dispatch.py` report cost as `n/a` and report
   token counts (`tokens_in`, `tokens_out`) and wall-clock duration (`duration_s`) instead.
5. **Sandbox policy.** `isolation = "docker"` is the default (`policy/sandbox.md`). Using
   `--isolation local` requires explicit `--allow-unsandboxed-local` or
   `allow_unsandboxed_local = true` in `.devpack/project.toml`.

## 3. Qualification gate before use

Before enabling a local engine (or after any model weight, quantization, or serving-stack
update), run single-mode evaluation on the `smoke` tier and verify all three criteria:

1. **Reliability:** `smoke` tier `pass^3 >= 0.8`.
2. **Tool protocol conformance:** `tool_protocol_errors == 0` across all trials.
3. **Code-quality conformance:** `lint_clean_final` on `>= 90%` of passing trials
   (`enforcement/lint/preflight_lint.py` clean after the runner's fix loop).

Every worker brief must embed the inline checklist (`runner/quality_checklist.md`) because
smaller local models do not reliably open external skill files on their own.
