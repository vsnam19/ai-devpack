# Install an offline release

Use a company-approved machine with Bash, Python 3.10 or newer, git, tar, and
`sha256sum`. Docker is optional for the self-test. Provision the approved
container runtime and preloaded images if you will run agents in containers.

## Verify and unpack

Transfer the archive and its checksum file together. Obtain the expected
checksum through an authenticated company channel before extracting. Replace
`VERSION` below with the release version and run these commands from the
directory holding both files.

```bash
sha256sum -c devpack-VERSION.sha256 &&
tar -xzf devpack-VERSION.tar.gz &&
cd devpack-VERSION &&
bash tests/run_all.sh
```

The checksum check prints `OK` for the archive. The self-test ends with
`ALL OK`. Keep the verified pack in an operator-controlled directory outside
agent worktrees for interactive use.

## Configure a project

Create `.devpack/` in the project repository. Copy the project gate template
from `enforcement/gate/gate.project.example.toml` to `.devpack/gate.toml` and
configure your checks and components. Use
[the project example](../.devpack/project.example.toml) as a starting point for
`.devpack/project.toml`:

```toml
data_class = "internal"
gate_config = "gate.toml"
engines_config = "engines.toml"
plans_root = "plans"

[default_engines]
implementer = "implementation"
reviewer = "review"
fixer = "implementation"
orchestrator = "review"

[pipeline]
mode = "hitl"
```

Set `data_class` to the company's approved classification for this project.
The gate and engine config paths resolve relative to `.devpack/project.toml`.
`plans_root` is project-relative and tells the dashboard where to discover
plans. Pass the actual plan directory to the plan CLI. Use `hitl` for human
checkpoints. Use `autonomous` only with approval for unattended operation.
Mandatory human checkpoints still apply. See [pipeline modes](../policy/operating_modes.md)
and the [project installation steps](../README.md#install-into-a-project).

Create `.devpack/engines.toml` using the
[engine template](../runner/engines.example.toml). For a company CLI, use
`class = "external"`. Set `command` to an argv array for the installed CLI or
an approved wrapper, passing the brief and working directory through placeholders.
Set `result` and `reply_from` to match the CLI's output, and set
`max_data_class`, `timeout_s`, and any required `env_allow` entries. Install
the executable wherever jobs run. For native engines, use the built-in local
adapter for an approved endpoint reachable on the offline network and configure
its model, endpoint, and data limit using the template. Use the native cloud
class only in a deployment with separately authorized connectivity. Map each
role to a configured engine and use an independent engine for review.

## Platform profile and dashboard

For the SELinux skill, copy the fictional platform profile linked from the
[skill instructions](../knowledge/skills/legato-selinux/SKILL.md). Fill in the
project's platform conventions and set `[selinux].platform_profile` and
`platform_root` according to the skill's workspace configuration.

From the pack directory, run
`python3 runner/devpack_dash.py --workdir /path/to/project` for the standard-library
dashboard. For the optional terminal dashboard, provision the
[terminal dashboard dependencies](../runner/dash/tui/app.py) in a separate environment from an
approved internal package mirror or from vendored wheels. With a wheel bundle,
install the approved requirements using
`python3 -m pip install --no-index --find-links /path/to/wheels -r /path/to/approved-requirements.txt`.
Then run `python3 -m runner.dash.tui --workdir /path/to/project` from the pack
directory using that environment.
