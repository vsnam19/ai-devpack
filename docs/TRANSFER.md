# Install and use an offline release

Use a company-approved machine with Bash, Python 3.10 or newer, Git, tar, gzip,
`sha256sum`, and the usual Linux coreutils. The installer runs the pack
self-test by default. Have ShellCheck, a C++17-capable `g++`, `gcov`, `nm`
(binutils), and `cppcheck` available. Provision an approved container runtime
and preloaded images if jobs use container isolation.

## Verify and install

Transfer the archive and checksum file together and obtain the expected digest
through an authenticated company channel. Replace `VERSION` with the release
version and run from the directory holding both files.

```bash
sha256sum -c devpack-VERSION.sha256 &&
tar -xzf devpack-VERSION.tar.gz &&
cd devpack-VERSION &&
./install.sh --verify ../devpack-VERSION.sha256
```

The checksum covers the archive. Extract into a trusted directory and keep the
installed pack outside project and worker worktrees. The installer needs no
network access or elevated privileges. It installs to
`$HOME/.local/share/devpack/<version>`, updates the `current` symlink, and
creates `$HOME/.local/bin/devpack`. Use `--prefix DIR` and `--bin-dir DIR`
to select different locations. Add the command directory to your PATH.

```bash
export PATH="$HOME/.local/bin:$PATH"
devpack version
```

The self-test ends with `ALL OK`. Use `--no-selftest` if the test tools are
unavailable and the release has already been verified on another machine.
To rerun the self-test later, use `devpack selftest`, or run this command
from the extracted pack.

```bash
bash tests/run_all.sh
```

To remove this version, run `./install.sh --uninstall` from the extracted
release, passing the same custom paths if used during installation.

## Check and scaffold the project

Run from the project repository. `doctor` warns when the project config is
missing. Choose the data classification approved for the project.

```bash
devpack doctor
devpack init --mode hitl --data-class internal
```

The scaffold includes `.devpack/project.toml`, `.devpack/engines.toml`,
`.devpack/gate.toml`, `plans/example/`, and a `/runs/` ignore entry.
Re-running init preserves existing project files. Use `--install-hooks` or
`--install-skills` to add the pre-commit hook or bundled skills. Use
`autonomous` mode only with approval for unattended operation. See
[pipeline modes](../policy/operating_modes.md).

## Configure engines and the gate

Edit `.devpack/engines.toml` for the approved tools available on your
company machine. For a company CLI, uncomment the external engine example
and use an argv array for `command`. Set the executable, brief and workdir
placeholders, output format, classification limit, and any environment
variable names needed by the tool. Map workflow roles to configured engines
and use an independent engine for review. Remove engine entries you will
never use. Install the executables wherever jobs run, including inside
the preloaded images if required. The doctor checks host binaries.

Set `[workspace]` and component commands in `.devpack/project.toml` and
review the checks in `.devpack/gate.toml` before executing jobs. See
[workspace scope](../policy/workspace_scope.md) and the
[project installation steps](../README.md#install-into-a-project).
Run `devpack doctor` again after editing the config. To validate the engine
file directly from the extracted pack, run
`python3 runner/agent_exec.py --check-config /path/to/project/.devpack/engines.toml`
or use `devpack skills --check-config .devpack/engines.toml` from the project.

## Run and inspect a plan

Review the example briefs in `plans/example/briefs/` and adapt them for
the project before committing the task baseline. Then run:

```bash
devpack plan run-next plans/example
devpack dash
```

Use `devpack tui` for the terminal dashboard when Textual is available in
the launcher’s Python environment. Both dashboards accept `--workdir DIR`
when launched outside the project.

## Build a release

With Git 2.31 or newer and ShellCheck installed, run
`bash tools/make_release.sh VERSION OUTDIR`.
The version must match a Git tag and the `VERSION` file at that tag. Tags
may start with `v`. Use `0.0.0-test` to smoke-test the committed HEAD.
