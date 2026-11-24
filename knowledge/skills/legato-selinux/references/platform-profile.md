# Platform profile contract

The adopting project supplies `[selinux] platform_profile = "profiles/platform.toml"`
in its project config. The path is workspace-root-relative, including when the project
config is elsewhere. Absolute paths outside the workspace, parent escapes and escaping
symlinks are rejected. The profile itself is TOML (Python 3.11+). It is data, never an
executable hook. Keep proprietary profiles outside the pack. The
[example](example-platform-profile.toml) uses fictional conventions only.

| Key | Type and meaning |
| --- | --- |
| `prefix` | Nonempty identifier prefix. |
| `domain_pattern`, `exec_type_pattern`, `data_type_pattern` | String templates containing `{app}` or `{executable}`; domain, entrypoint and private-data type naming. Use executable identity when apps have several executables. |
| `admin_source_domain` | Identifier for the loader/supervisor source domain. |
| `transition_check` | String template for the transition the selected loader checks in converted/linked policy; evaluate with app identity separately from executable naming. |
| `module_files` | Table with `te`, `fc`, `if`, `makefile`, `component` filename templates. Filenames cannot contain path separators or parent escapes. |
| `module_bundle_path` | Relative string template for the .pp location within the app image; absolute paths and parent escapes are invalid. Distinguish bundle destination from installation pathname. |
| `skeleton_generator` | Optional command template; tool pathname relative to platform root, explicit input/output arguments. Absence means no known generator, not an invented command. |
| `file_contexts_tool` | Optional command template relative to platform root; document actual input/output behaviour. |
| `interfaces` | Nonempty table mapping manifest item kinds to macro invocation/argument string templates. Multiple semantically distinct operations need separate keys; raw rules may be documented here when no helper exists. Missing per-kind entries are unknown for that operation. |
| `base_inherited_grants` | Array of nonempty grant/scope descriptions with base-policy evidence. Every entry requires a separate manifest deviation. Empty array asserts no known grants and still needs linked-policy verification. |
| `banned` | Array of platform anti-pattern descriptions. Supplements universal safety rules; cannot relax them. |
| `build` | Table with `include_variables`, a nonempty array of Makefile variable identifiers needed to locate/configure SDK includes. Values and external include expansions need build evidence. |

Templates accept only plain placeholders: `prefix`, `app`, `executable`, `domain`,
`exec_type`, `data_type`, `admin_source_domain`, `client`, `server`, `type`, `receiver`,
`creator`, `cap`, `adef`, `output`, `platform_root`. No conversions, format specs,
attribute/index traversal or expression evaluation. Make variables belong in build
metadata, not brace templates. Substitution never grants authorization or implies
shell quoting: inspect arguments before any separately authorized command execution.

Suggested interface keys are `entrypoint`, `admin_registration`, `file_read`, `file_rw`,
`private_data`, `ipc_client`, `config_tree`, `network`, `network_bind`, `shell_exec`,
`library_read`, `proc_global`, `proc_state`, `vm_overcommit`, `logs`. Additional named
kinds can describe platform-specific catalog entries. A server export has no assumed
universal authorization helper. Device ioctl filtering and FD object access require
explicit policy design and BSP verification.

`python3 scripts/extract_manifest.py` loads and validates profiles through
`scripts/platform_profile.py`. Missing/unreadable/malformed profiles, absent required
keys, wrong types and invalid nested values become `unknown` entries in QUESTIONS.md;
valid values remain available for review. Optional tools are validated when present.
Malformed workspace/config paths exit 2. The extractor records the validated contract
in questions, seeds one draft `inherited_grants` record per base entry with a stable
`BASE-NNN` deviation ID, and asks for actual source evidence and affected processes.
Profile line 1 is provenance for those draft records, not proof of a base-policy grant.
Reconcile additional linked-policy grants, approve each deviation explicitly, and resolve
all applicable unknowns before generation/build/release. It never substitutes a fictional
example, runs profile commands or turns missing fields into guessed conventions.
