# Manifest to platform module mapping

Read the adopting project's [platform profile](platform-profile.md) first. Resolve
interface templates from its `interfaces` map, never from an example or a remembered
vendor convention. D is an actual process domain, T a verified object type, S the
exporting peer domain. Require external types with `gen_require`. Every argument
needs approved evidence; a pathname or API name is not a type.

| Manifest kind | Profile mapping / review boundary |
| --- | --- |
| Process entrypoint | `entrypoint`, `admin_registration`; derive domain/exec/data names from profile patterns for each executable. Independently satisfy app-name `transition_check`. |
| File read / read-write | `file_read`, `file_rw`; verify parent traversal and actual labels. Creation/removal need management rules. |
| Private data | `private_data`; inspect transitions and generic migration grants as deviations. |
| Device/ioctl | No assumed helper: exact typed chr_file permissions; verify xperm support and ioctl constants with BSP. |
| Unix socket | Exact sock_file path access, self creation and peer permissions; IPC binding is not universal socket authorization. |
| IPC client | `ipc_client`; resolve the actual exporting executable, with required external types. |
| IPC server/export | Initialize/label server and authorize client-to-server access in clients; an export alone grants no callers. |
| FD passing | Exact receiver-to-creator fd use plus underlying object permissions; reverse/third-party origins are separate. |
| TCP/UDP | `network`, optionally `network_bind`; constrain port types and name_bind/name_connect. Creation and listening are separate. |
| Capability | Exact self capability/capability2 permissions; Linux capability declarations remain separate. |
| Exec/helper | Entrypoint transition and typed helper rules; `shell_exec` only for an approved shell need. |
| Shared library | `library_read` if supplied; inspect exact read/map/execute expansion in SDK. |
| proc/sys | `proc_global`, `proc_state`, `vm_overcommit`; approve full scope and account for inherited access. |
| Config tree | `config_tree` plus definition binding and tree-path rights; IPC does not isolate tree paths. |
| Logs/stdout | `logs` and inherited log/pipe grants; expand base policy before approval. |
| Runtime tmpfs/memfd/socketpair | Verify creator domain, transitions, object classes and FD use independently. |
| External labels | BSP setfiles/udev integration; verify device recreation/boot and image labels. |
| env/limits/identity/denied | Runtime/sandbox configuration and negative tests, not invented allow rules. |

If an interface key is absent, mark it unknown in QUESTIONS.md before dependent work;
choose a precise raw rule only after BSP evidence and human approval. These are design
requirements, not claims that a particular platform implements the macro or raw rule.

## Module and image contract

Use `module_files` names and `build.include_variables` with the matching SDK include.
Attach the policy component to the app and verify `module_bundle_path` in the actual
image. A generator is optional and yields only a skeleton; do not rename executables
to accommodate it. Resolve commands relative to `platform_root` without executing them
during extraction. Never assume generated modules automatically ship.

Check converted/linked policy against `transition_check` from `admin_source_domain`;
app-name checks and executable domains can differ. Verify the selected loader revision,
including loading/label failure handling, before claiming confinement.

Inspect nonempty aggregate file_contexts and applied image labels. If supplied, review
`file_contexts_tool` filtering, ordering and fallback behaviour. Empty contexts fail the
build even if image tooling accepts them. Pre-link approved modules where possible;
verify loaded modules and domains after update/reboot. Profile content and a successful
source build are not evidence of the final linked permissions or runtime confinement.

Use `python3 scripts/gen_policy.py --manifest approved.json --profile platform.toml --out module`.
The implemented category-to-interface keys and explicit argument bindings are listed
in [tooling](tooling.md). Unknown kinds produce TODOs, QUESTIONS.md and exit 3.
