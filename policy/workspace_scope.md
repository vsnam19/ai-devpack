# Workspace and component scope (Yocto repositories)

In a Yocto project, an organisation typically owns one layer or source subtree inside a
much larger repository containing upstream layers (`poky/`), third-party BSP layers
(`meta-*`), and BitBake output directories (`build/`, `tmp/`, `downloads/`,
`sstate-cache/`).

`runner/workspace_scope.py` bounds every `ai-devpack` check, symbol index, brief
validation, and worker edit to the configured `[workspace]` root and the active set of
components. When `[workspace]` is omitted from `.devpack/project.toml`, the entire
repository is treated as a single implicit component (legacy behaviour).

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

[[component]]
name = "ipc_bus"
path = "middleware/ipc_bus"
compile_commands = "build/ipc_bus/compile_commands.json"
include_dirs = ["middleware/ipc_bus/include"]
build_cmd = "devtool build ipc-bus"
test_cmd = "ctest --test-dir build/ipc_bus --output-on-failure"

[scan]
default = "selected"   # "selected" | "all"
selected = ["ipc_bus"]
```

### Automatic component discovery

When `[workspace]` is configured with no `[[component]]` tables, direct subdirectories of
`workspace.root` containing `CMakeLists.txt`, `Makefile`, `meson.build`, `*.bb`, or
`*.bbappend` are discovered as components automatically using their directory name.

## What is never scanned or edited

- Anything outside `[workspace].root` (`poky/`, `meta-*`, `build/`, `tmp/`,
  `downloads/`, `sstate-cache/`) is never walked by `runner/repomap.py`,
  `enforcement/lint/preflight_lint.py`, or `enforcement/static/cppcheck_check.py`.
- Paths matching `[workspace].exclude` (such as `**/third_party/**` and
  `**/generated/**`) are excluded even inside active components.
- `runner/brief_lint.py` errors on `Files allowed` globs outside `workspace.root`.
- `enforcement/checks/scope_check.py` and `runner/devpack_dispatch.py` refuse any worker
  edit outside `workspace.root` regardless of `--allow` globs.

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

`enforcement/static/cppcheck_check.py` passes `--project=<compile_commands>` (with
`--suppress=missingIncludeSystem`) when a component's `compile_commands` file exists, and
falls back to the component's `include_dirs` otherwise.

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
