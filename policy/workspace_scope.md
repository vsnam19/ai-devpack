# Workspace and component scope (Yocto repositories)

In a Yocto project, an organisation typically owns one layer or source subtree inside a
much larger repository containing upstream layers (`poky/`), third-party BSP layers
(`meta-*`), and BitBake output directories (`build/`, `tmp/`, `downloads/`,
`sstate-cache/`).

`runner/workspace_scope.py` bounds every `ai-devpack` check, symbol index, brief
validation, and worker edit to the configured `[workspace]` root and the active set of
components. When `[workspace]` is omitted from `.devpack/project.toml`, the entire
repository is treated as a single implicit component by standalone tools (legacy
behaviour). The installed project gate template requires an explicit `[workspace]`
and active components; legacy mode does not apply to `project_checks.py`.

## Yocto layout and `.devpack/project.toml`

```text
repo-root/
  poky/                               # NEVER scanned, indexed, or edited
  meta-other/                         # NEVER scanned, indexed, or edited
  build/
    tmp/                              # NEVER scanned, indexed, or edited
    ipc_bus/compile_commands.json     # Read-only compile DB referenced by [[component]]
  meta-org/
    recipes-org/
      src/                            # [workspace].root
        middleware/ipc_bus/
        services/diag_svc/
        apps/sensor_hub/
  .devpack/
    project.toml
```

Configure `.devpack/project.toml` from `.devpack/project.example.toml`:

```toml
[workspace]
root = "meta-org/recipes-org/src"
exclude = ["**/third_party/**", "**/generated/**"]
discover = true
discovery_depth = 4

[[component]]
name = "ipc_bus"
path = "middleware/ipc_bus"
compile_commands = "build/ipc_bus/compile_commands.json"
build_dir = "build/ipc_bus"
test_xml = "junit.xml"
include_dirs = ["middleware/ipc_bus/include"]
build_cmd = "devtool build ipc-bus"
test_cmd = "ctest --test-dir build/ipc_bus --output-on-failure"

[scan]
default = "all"        # use "selected" only for a local fast loop
selected = ["ipc_bus"]
```

### Automatic component discovery

With `[workspace] discover = true` (the default), discovery descends recursively up
to `discovery_depth = 4`, stopping at a discovered component. Directories containing
`CMakeLists.txt`, `Makefile`, `meson.build`, `*.bb`, or `*.bbappend` identify components
using their directory name. Thus the nested `middleware/ipc_bus`, `services/diag_svc`,
and `apps/sensor_hub` layout above is discovered when each leaf has a build marker.
Explicit `[[component]]` entries merge with discovery and supply build/test metadata;
set `discover = false` to require only explicit components.

## What is never scanned or edited

- Anything outside `[workspace].root` (`poky/`, `meta-*`, `build/`, `tmp/`,
  `downloads/`, `sstate-cache/`) is never walked by `runner/repomap.py`,
  `enforcement/lint/preflight_lint.py`, or `enforcement/static/cppcheck_check.py`.
- Paths matching `[workspace].exclude` (such as `**/third_party/**` and
  `**/generated/**`) are excluded even inside active components.
- `runner/brief_lint.py` errors on `Files allowed` globs outside `workspace.root`.
- `enforcement/checks/scope_check.py` and `runner/devpack_dispatch.py` refuse any worker
  edit outside `workspace.root` regardless of `--allow` globs, and refuse any worker edit
  to `preflight.toml`, `.devpack/**`, or gate/lint config files as `gate-touched`.
- `runner/workspace_scope.py`, `runner/repomap.py`, and `enforcement/static/cppcheck_check.py`
  resolve workspace root, component directories, and `compile_commands.json` entries and
  fail closed if any symlink or path escapes the repository or workspace root.
- An explicitly requested `--project-config` or broker-provided config that is missing or
  unparsable fails with exit code `2` (never falling back to legacy whole-repo scope).
- `runner/devpack_dispatch.py` further bounds worker edits to the active component set
  (the union of selected components and components explicitly named by `--allow` globs).
  Any changed file dropped by component/exclude filtering is recorded in `run.json` as
  `out_of_scope_changes` and fails the gate.
- `runner/devpack_submit.py` and `runner/devpack_broker.py` accept `--components` /
  `--all-components`, validate component names against the trusted host project config,
  and copy `[workspace]`, `[[component]]`, and `[scan]` into the sandbox `project.toml`.

## Selecting components (priority order)

1. CLI flags `--components a,b` or `--all-components` on:
   - `runner/workspace_scope.py` (`resolve`, `filter`)
   - `enforcement/lint/preflight_lint.py`
   - `enforcement/static/cppcheck_check.py`
   - `runner/repomap.py`
   - `runner/devpack_dispatch.py`
   - `runner/devpack_run.py`
   - `enforcement/gate/gate.py` (sets `DEVPACK_COMPONENTS` for child checks)
2. Environment variable `DEVPACK_COMPONENTS` (`all` or comma-separated component names).
3. `[scan]` defaults in `.devpack/project.toml` (`default = "selected"` or `"all"`).

Unknown component names fail closed with an error listing all valid component names.
`run.json` records `components` and `scan_mode` (`"selected"`, `"all"`, or `"legacy"`).

## Generating `compile_commands.json` for a component

`enforcement/static/cppcheck_check.py` builds a filtered temporary compile database
containing only entries inside the active component and outside `[workspace].exclude`,
passes `--project=<filtered_compile_commands>` (with `--suppress=missingIncludeSystem`),
and analyses any component source files missing from the database through the
`include_dirs` fallback path (reporting the fallback file count). When `compile_commands`
is absent, all component files use `include_dirs`.

Two common ways to generate `compile_commands.json` in a Yocto build (external to the
pack; verify against your BitBake/eSDK setup):

1. **Using `devtool` / recipe CMake build directory**:
   - Add `EXTRA_OECMAKE += "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"` in the recipe or
     `devtool` workspace append, run `devtool build <recipe>`, and point
     `compile_commands` at the generated file in the recipe's build tree (or copy it to
     `build/<component>/compile_commands.json`).
2. **Using the Yocto eSDK**:
   - Source the eSDK environment script, configure the component with
     `cmake -S <component-src> -B build/<component> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`,
     and point `[[component]].compile_commands` at `build/<component>/compile_commands.json`.

## Empty scans and CI coverage

Lint, cppcheck, and the gate fail with `NO_FILES_CHECKED` when an expected scan finds
no files. Explicit files outside active components or matching workspace exclusions
fail as `OUT_OF_SCOPE`; they are never silently skipped. `--all-components` expands
component selection while preserving workspace boundaries and exclusions.

Selected scans print `WARNING: N configured components not checked: …` and record
the omitted components in run metadata/receipts. Use selection for local fast loops;
CI must pass `--all-components`. An empty changed-C/C++ set may explicitly use
`--allow-empty`, producing `SKIP: no C/C++ changes`.

Pack self-tests select their own configuration through `DEVPACK_PROJECT_CONFIG=none`
or `--project-config none`, so a vendored pack never inherits the parent product scope.

## Optional Legato SELinux skill configuration

The `legato-selinux` skill reads the following optional project section independently
of component scope resolution. The workspace loader does not interpret these keys.
Do not hardcode a company tree layout; reconcile selected components and exclusions
before inventory. See [the skill](../knowledge/skills/legato-selinux/SKILL.md).

```toml
[selinux]
platform_profile = "profiles/platform.toml"
platform_root = ""
app_globs = ["**/apps/*.adef"]
api_search_paths = ["interfaces"] # Workspace-relative directories, in search order
sdef_globs = [] # Optional workspace-relative .sdef patterns
manifest_name = "access-manifest.yaml"
module_dir = "{app_dir}/../selinux"
sdk_policy_include = ""
linked_policy = ""
exclude = []
```

Paths and globs are relative to the workspace root, including `platform_root`, SDK and
linked policy. Empty paths mean unknown and block dependent policy/build validation.
`app_globs` discovers definitions for `--app-name`; multiple matches require an explicit
.adef. `exclude` removes matching workspace-relative references. `manifest_name` is a
plain output filename. `module_dir` is a policy-authoring template: replace `{app_dir}`
with the workspace-relative app directory, normalize, and reject workspace escapes.
The extractor does not create modules. It accepts JSON config with the same `selinux`
object when TOML support is unavailable, and writes draft YAML using JSON flow syntax.

`platform_profile` selects a project-owned TOML file relative to the workspace root.
See the [profile contract](../knowledge/skills/legato-selinux/references/platform-profile.md).
Naming, interfaces, build conventions, inherited grants and loader checks come only
from that profile. Missing or invalid fields become unknowns in QUESTIONS.md and
block dependent steps. Keep proprietary profiles outside the pack in ignored project
research or configuration storage; the shipped example is fictional.


## SELinux extractor configuration

The skill reads `[selinux]` independently of component selection. Optional
`sim_macros = ["PROJECT_REPLAY"]` adds exact C preprocessor identifiers for project
simulation branches to the built-in SIMULATION/TEST/MOCK recognition. Such evidence
is `candidate_denied` and draft; it never authorizes a production grant or confirms a
denial. `#else` production evidence remains in ordinary draft resource categories.
See [Legato SELinux](../knowledge/skills/legato-selinux/SKILL.md) for the full config.

API lookup tries the component `.cdef` directory, then `api_search_paths` in order,
then `interfaceSearch` entries from definitions matched by optional `sdef_globs`.
Patterns follow configuration order; matching definitions are sorted by path and entries
retain declaration order. `interfaceSearch` entries are relative to their `.sdef` directory.
All resolved references, including symlinks, must remain inside the workspace and respect
`exclude`. No workspace-wide API fallback is used. Unresolved APIs remain unknown with
a question naming the API and every attempted path. Variables, includes and conditional
build expansion require manual reconciliation; no build flags are read automatically.
