# Agent logging policy

Every agent execution produces a structured run directory so that the repository owner,
colleagues, and reviewers can inspect the exact prompt, tool calls, tool outputs, gate
results, final answer, token usage, duration, and cost without special tooling.

## 1. What is logged

Each run writes a directory under `runs/<run-id>/` (where `<run-id>` is a UTC timestamp
plus random hex suffix, `yyyymmddThhmmssZ-<6hex>`):

| Path | Content |
|---|---|
| `runs/<run-id>/run.json` | Run metadata: `run_id`, `kind` (`task`, `eval`, `dispatch`, `skilltest`), `started`, `finished`, `engines`, `task`, `outcome`, total cost and duration |
| `runs/<run-id>/steps/NN-<role>-<engine>.prompt.md` | Exact prompt sent to the agent for step `NN` |
| `runs/<run-id>/steps/NN-<role>-<engine>.jsonl` | Redacted raw `stream-json` event stream from the agent CLI |
| `runs/<run-id>/steps/NN-<role>-<engine>.meta.json` | Step execution summary: status, exit code, model, duration, turns, tokens in/out, cost in USD, tool-call counts, `tool_protocol_errors`, and prompt SHA-256 |
| `runs/<run-id>/steps/NN-gate.txt` | Deterministic gate output when a gate check ran at step `NN` |
| `runs/<run-id>/steps/NN-gate-receipt.json` | Machine-readable receipt from the pack gate at step `NN`, including each gate attempt after a fix |
| `runs/<run-id>/transcript.md` | Human-readable Markdown transcript rendered across all steps in order |
| `runs/<run-id>/index.html` | Self-contained static HTML page (inline CSS, light/dark mode, no JavaScript or external assets) |

Logs are rendered, listed, and indexed with `runner/devpack_logs.py`:

```bash
python3 runner/devpack_logs.py render runs/<run-id>
python3 runner/devpack_logs.py render --session session.jsonl --out runs/session-view
python3 runner/devpack_logs.py show runs/<run-id> --step 01
python3 runner/devpack_logs.py list runs
python3 runner/devpack_logs.py site runs
```

## 2. Confidentiality and data governance

Agent logs routinely contain source code excerpts, file paths, diffs, requirement text,
and test outputs read or produced during the task.

- **Same classification as the repository.** Every run directory and rendered transcript
  inherits the confidentiality class of the repository it ran against (for example,
  `Internal` or `Confidential`; see `policy/data_governance.md`).
- **Governance compliance.** Storage, access control, and transfer of logs must obey
  `policy/data_governance.md` and internal provenance rules in `policy/provenance.md`.
- **Automatic redaction.** Redaction is applied both when recording raw `.jsonl` streams
  and when rendering Markdown/HTML via `runner/devpack_logs.py`:
  - Built-in secret detection rules and external regex pattern files loaded via
    `enforcement/checks/secret_pii_check.py`.
  - Values of environment variables passed via `--redact-env NAME` and configured
    authentication token environment variables.
  - Credential-looking strings (bearer tokens, API key prefixes, secret assignments).
  - Absolute user home directory paths, which are normalized to `~`.
- Redaction is a defense-in-depth safeguard against accidental credential exposure; it
  does not downgrade the confidentiality class of the run log.

## 3. Who may read

- The project owner, developers, reviewers, and internal auditors who are authorized to
  read the repository at its confidentiality class under `policy/data_governance.md`.
- Logs must not be published on public hosts or sent to external parties or customers
  without explicit approval from the data owner (`policy/data_governance.md` section 5).

## 4. Retention and repository hygiene

- **Default retention: 90 days.** Run directories under `runs/` are retained for 90 days
  by default and then purged, unless linked to a long-term release or safety evidence
  bundle under `policy/provenance.md`.
- **Never commit `runs/`.** Run directories are operational artifacts and must never be
  committed to git (`runs/` must remain ignored in `.gitignore`). Pre-commit scanning via
  `enforcement/checks/secret_pii_check.py` remains mandatory on all staged commits.

## 5. How to share

To share an agent run with the project owner or a colleague who has repository access:

1. **Copy the run directory:** archive or copy `runs/<run-id>/` (containing
   `transcript.md`, `index.html`, and `steps/`) to an access-controlled location.
2. **Publish static HTML on the internal share:** copy `runs/<run-id>/index.html` (or
   generate a multi-run static site with `python3 runner/devpack_logs.py site runs` and
   publish the `runs/` directory) to the project's internal file or web share that enforces
   the repository's confidentiality class. Because `index.html` is a single self-contained
   file with inline CSS and no external dependencies, colleagues can open it directly in
   any browser.

### External engine steps

The standard step files contain the redacted effective brief, JSONL evidence,
and result metadata. The transcript copies JSON object events from the optional
`events_from` path after the job exits. Jobs that buffer their output are
supported. When no events are available, one synthetic result event records
the reply. Keep reply and event files under the private step output directory
and use the pack step files for persistent evidence. The pack records unknown
token counts and cost as JSON null. Quota and exhaustion map to
`engine_unavailable` with an ENV/TOOL attribution hint. An external job's gate
status is execution evidence. The pack records its own gate outcome separately.
