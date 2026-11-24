# Offline tooling contract

All commands use stdlib Python 3.10+, JSON flow YAML manifests and project-owned TOML
profiles. Existing schema and required profile keys are unchanged. No script uses
network services, audit2allow, vendor base-policy edits or automatic permission growth.

## Generator

`python3 scripts/gen_policy.py --manifest approved.json --profile platform.toml --out module`
requires all records approved and root approval metadata identifying the exact version.
It rejects unresolved root unknowns and duplicate/unsafe IDs. Use a new/empty output.
Only explicitly reviewed profile interface strings supply policy statements. Every
statement and context carries `# M:<ID>`; runtime configuration, denied operations and
inherited-grant records get explicit `no-rule-needed` reasons. Inherited grants are
review obligations, not locally regenerated allows.

Mappings: processes → entrypoint/admin_registration; files → file_read/file_rw;
data_dirs → private_data; devices → device_ioctl; unix_sockets → unix_socket;
ipc → ipc_client/ipc_server; fd_passing → fd_passing; network → network;
capabilities → capability; exec → helper_exec; proc_sys → proc_global;
config_tree → config_tree. An absent mapping or unresolved argument yields TODO,
QUESTIONS.md and exit 3. No lexical pathname becomes an inferred SELinux type.
Different library/proc/network suboperations must be reconciled into reviewed profile
mappings before generation; the extractor is not a build-language evaluator.

Optional tooling fields are explicit project data, never fallback conventions:

```toml
# Top-level fields, before any table:
baseline = "effective-baseline.json" # relative to profile directory; check into project
# Capture from the release target, archive provenance/time/build alongside this file.
enforcing_evidence = "getenforce.txt" # contains exactly Enforcing
platform_root = "/reviewed/platform" # only needed for optional skeleton command
adef = "/reviewed/apps/sample.adef" # only needed if skeleton template uses it

[contexts]
installed = "/framework/apps/{app}/read-only/bin/{executable} -- gen_context(system_u:object_r:{exec_type},s0)"
writable = "/framework/appsWriteable/{app}(/.*)? gen_context(system_u:object_r:{data_type},s0)"

[bindings.FILE-001]
type = "sample_config_t"
class = "file"
permissions = ["read"]

[bindings.IPC-001]
server = "peer_t"

[bindings.PROCESS-001]
type = "sample_app_t" # explicit admin_registration argument
```

Bindings are keyed by manifest ID and reviewed with its version. Supported template
arguments (`type`, `server`, `receiver`, `creator`, `cap`) remain those in [platform profile](platform-profile.md); class/permissions
are additional gate/triage selectors, not template fields. All binding identifiers and
permissions must be plain SELinux identifiers. External bound types receive
`gen_require` declarations. `interfaces.app_transition` supplies actual source syntax;
`transition_check` supplies its expected semantic tokens, evaluated separately with app
identity. For a multi-executable app, explicitly approve the app transition separately.

`contexts.installed` and `contexts.writable` are full file-context line templates;
both are required for complete generation. Identical context regex/selector entries
with conflicting labels are omitted, reported as TODOs and return exit 3. External paths under /etc, /data, /var/lib
and /dev use explicit `type` bindings and go only in `<app>.platform.fc`. BSP owners
must review context syntax/order and integrate that file in image labeling.

An optional `skeleton_generator` executes via argv (no shell), with a 60-second timeout,
relative to explicit `platform_root`; `{output}` is a fresh temporary directory. Only
single-line `type identifier;` declarations from generated `.te` files are merged;
its grants, transitions, contexts, build output and interfaces are discarded. The module
header uses the approved app identity. The hook is skipped entirely when absent.
Review the command itself before execution; it is trusted project code, not a sandbox.

The Makefile lists required SDK include variables and appends the explicit optional
`build.makefile` string template (for example, the fictional profile includes
`$(SHAREDIR)/example-policy.mk`). It never invents an SDK include path or compiler
invocation. Missing `build.makefile` produces a TODO, exit 3 and a Makefile error;
supply reviewed SDK build rules before compilation. The
component declares the module at `module_bundle_path`; app attachment and actual image
integration still need build evidence. Generated source is never proof of a loadable
module, correct linked policy or release readiness.

## Gate

`python3 scripts/sel_gate.py --module module --manifest approved.json --profile platform.toml`
prints JSON. It checks provenance on every non-comment .te/.if statement, item coverage,
profile bans/deviation IDs (`# D:<deviation_id>`), the app transition, declared bundle,
required module files and nonempty framework contexts. Universal permissive, unconfined
and dontaudit bans cannot be waived. Profile-specific banned strings require an approved
inherited-grant deviation ID on the same source line. TODOs fail the gate.

`--linked-policy linked.policy` runs SETools `sesearch -A -s DOMAIN` for each process
domain and `sesearch --allowxperm -s DOMAIN` for extended permissions. The baseline JSON maps each domain to sorted, whitespace-normalized complete
`allow`/`allowxperm` source lines, including inherited/conditional rules. Review and
check this file into the adopting project with manifest/deviation evidence. Any difference
fails; the gate never updates the baseline. Each denied item needs explicit process,
`type`, `class`, `permissions` selectors; each query must return zero allow rules.
`seinfo --permissive` must report zero permissive types. Tool errors fail.

Exit 0 PASS requires source and linked checks. Exit 3 TOOL_MISSING means SETools or
linked policy is absent. `--release` turns either absence into exit 1 FAIL and also
requires the captured Enforcing evidence. This evidence is not a live target probe;
perform and archive all [release checks](review-checklist.md). Source syntax checks
cannot replace SDK compilation, macro expansion, labeling or live target tests.

## Triage

`python3 scripts/avc_triage.py --log audit.log --manifest manifest.json --profile platform.toml`
reads audit/dmesg AVC records and writes exclusive new `audit.log.triage.json` and
`audit.log.draft-patch.json`. Override with `--report-out`/`--patch-out`; existing files
are refused. JSON stdout groups entries by source domain and names the patch file.
The partial manifest patch contains draft resource arrays for human merging, with the
raw AVC in `expression` and log line evidence. It never modifies manifest or policy.
See [classification](avc-triage.md); unknown class/origin/intent needs human investigation.
