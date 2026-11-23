"""Unit and integration tests for Yocto workspace component scoping (T21)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from enforcement.checks import scope_check  # noqa: E402
from enforcement.gate import gate  # noqa: E402
from enforcement.lint import preflight_lint  # noqa: E402
from enforcement.static import cppcheck_check  # noqa: E402
from runner import agent_exec, brief_lint, devpack_dispatch, devpack_run, repomap, workspace_scope  # noqa: E402

YOCTO_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "yocto_ws"


def _init_git_from_fixture(dst: Path) -> None:
    """Copy tests/fixtures/yocto_ws into ``dst`` and initialize it as a git repo."""
    shutil.copytree(YOCTO_FIXTURE, dst)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "tester@example.invalid"],
        ["git", "config", "user.name", "Tester"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "init yocto fixture"],
    ):
        subprocess.run(cmd, cwd=dst, check=True, capture_output=True)


def _write_fake_agent(path: Path, body: str) -> Path:
    """Create an executable fake claude CLI script returning stream-json."""
    path.write_text("#!/usr/bin/env python3\nimport json, pathlib, sys\n" + body, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class WorkspaceScopeTests(unittest.TestCase):
    """Verify workspace_scope resolution, discovery, filtering, and all pack integrations."""

    def test_yocto_fixture_resolution_priority_and_unknown_error(self) -> None:
        """Priority is CLI > DEVPACK_COMPONENTS > [scan], and unknown names list valid ones."""
        # Default [scan]: selected = ["ipc_bus"]
        scope_default = workspace_scope.resolve_workspace_scope(YOCTO_FIXTURE, env={})
        self.assertTrue(scope_default.configured)
        self.assertEqual("selected", scope_default.scan_mode)
        self.assertEqual(["ipc_bus"], [c.name for c in scope_default.active_components])
        self.assertEqual(
            ["meta-org/recipes-org/src/middleware/ipc_bus/**"],
            scope_default.path_globs(),
        )

        # Env override: DEVPACK_COMPONENTS=diag_svc,sensor_hub and DEVPACK_COMPONENTS=all
        scope_env = workspace_scope.resolve_workspace_scope(
            YOCTO_FIXTURE,
            env={"DEVPACK_COMPONENTS": "diag_svc,sensor_hub"},
        )
        self.assertEqual("selected", scope_env.scan_mode)
        self.assertEqual(["diag_svc", "sensor_hub"], [c.name for c in scope_env.active_components])

        scope_env_all = workspace_scope.resolve_workspace_scope(
            YOCTO_FIXTURE,
            env={"DEVPACK_COMPONENTS": "all"},
        )
        self.assertEqual("all", scope_env_all.scan_mode)
        self.assertEqual(["ipc_bus", "diag_svc", "sensor_hub"], [c.name for c in scope_env_all.active_components])

        # CLI overrides env
        scope_cli = workspace_scope.resolve_workspace_scope(
            YOCTO_FIXTURE,
            components="sensor_hub",
            env={"DEVPACK_COMPONENTS": "diag_svc"},
        )
        self.assertEqual(["sensor_hub"], [c.name for c in scope_cli.active_components])

        scope_cli_all = workspace_scope.resolve_workspace_scope(
            YOCTO_FIXTURE,
            all_components=True,
            env={"DEVPACK_COMPONENTS": "ipc_bus"},
        )
        self.assertEqual("all", scope_cli_all.scan_mode)
        self.assertEqual(3, len(scope_cli_all.active_components))

        # Unknown component errors and lists valid component names
        with self.assertRaises(ValueError) as ctx:
            workspace_scope.resolve_workspace_scope(YOCTO_FIXTURE, components="bogus_comp", env={})
        err_msg = str(ctx.exception)
        self.assertIn("bogus_comp", err_msg)
        for valid_name in ("ipc_bus", "diag_svc", "sensor_hub"):
            self.assertIn(valid_name, err_msg)

    def test_filter_and_outside_workspace_predicates(self) -> None:
        """filter() keeps only active component files outside excludes; outside_workspace() bounds to root."""
        scope = workspace_scope.resolve_workspace_scope(YOCTO_FIXTURE, components="ipc_bus", env={})
        self.assertTrue(scope.outside_workspace("poky/meta/recipes-core/bad_poky.c"))
        self.assertTrue(scope.outside_workspace("meta-other/recipes-vendor/bad_other.cpp"))
        self.assertTrue(scope.outside_workspace("build/tmp/work/bad_tmp.cpp"))
        self.assertTrue(scope.outside_workspace("../escape.cpp"))
        self.assertFalse(scope.outside_workspace("meta-org/recipes-org/src/middleware/ipc_bus/src/ipc_bus.cpp"))

        candidates = [
            "poky/meta/recipes-core/bad_poky.c",
            "meta-other/recipes-vendor/bad_other.cpp",
            "build/tmp/work/bad_tmp.cpp",
            "meta-org/recipes-org/src/middleware/ipc_bus/src/ipc_bus.cpp",
            "meta-org/recipes-org/src/middleware/ipc_bus/third_party/vendor_stub.cpp",
            "meta-org/recipes-org/src/services/diag_svc/src/diag_svc.cpp",
        ]
        self.assertEqual(
            ["meta-org/recipes-org/src/middleware/ipc_bus/src/ipc_bus.cpp"],
            scope.filter(candidates),
        )

    def test_component_discovery_without_explicit_component_tables(self) -> None:
        """Without [[component]], direct subdirs with CMakeLists.txt, Makefile, meson.build, or *.bb/*.bbappend are discovered."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".devpack").mkdir()
            (root / ".devpack" / "project.toml").write_text(
                '[workspace]\nroot = "meta-org/src"\n',
                encoding="utf-8",
            )
            ws = root / "meta-org" / "src"
            for comp_name, marker in (
                ("alpha_cmake", "CMakeLists.txt"),
                ("beta_make", "Makefile"),
                ("gamma_meson", "meson.build"),
                ("delta_recipe", "delta_1.0.bb"),
                ("epsilon_append", "epsilon_%.bbappend"),
            ):
                d = ws / comp_name
                d.mkdir(parents=True)
                (d / marker).write_text("# marker\n", encoding="utf-8")
            (ws / "docs_only").mkdir()
            (ws / "docs_only" / "README.md").write_text("# not a component\n", encoding="utf-8")

            cfg = workspace_scope.load_workspace_config(root)
            self.assertTrue(cfg.configured)
            self.assertEqual(
                ["alpha_cmake", "beta_make", "delta_recipe", "epsilon_append", "gamma_meson"],
                [c.name for c in cfg.components],
            )
            self.assertTrue(all(c.discovered for c in cfg.components))

    def test_legacy_behaviour_without_workspace_section(self) -> None:
        """Without [workspace], load_project_config and workspace_scope preserve legacy whole-repo behaviour."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".devpack").mkdir()
            (root / ".devpack" / "project.toml").write_text('data_class = "internal"\n', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

            proj = agent_exec.load_project_config(root)
            self.assertEqual({}, proj["workspace"])
            self.assertEqual([], proj["components"])

            scope = workspace_scope.resolve_workspace_scope(root, env={})
            self.assertFalse(scope.configured)
            self.assertEqual("legacy", scope.scan_mode)
            self.assertEqual(["**"], scope.path_globs())
            self.assertFalse(scope.outside_workspace("src/main.cpp"))
            self.assertTrue(scope.outside_workspace("../outside.cpp"))
            self.assertEqual(["src/main.cpp"], scope.filter(["src/main.cpp"]))

    def test_repomap_respects_selected_and_all_components_and_never_lists_poky_or_build(self) -> None:
        """repomap indexes only active components and excludes poky/, meta-other/, build/, and third_party/."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVPACK_COMPONENTS", None)
            md_sel = repomap.generate_repomap(YOCTO_FIXTURE, budget=2500)
            self.assertIn("IpcChannel", md_sel)
            self.assertNotIn("DiagManager", md_sel)
            self.assertNotIn("SensorHub", md_sel)
            for forbidden in ("poky/", "meta-other/", "build/", "third_party/"):
                self.assertNotIn(forbidden, md_sel)

            md_all = repomap.generate_repomap(YOCTO_FIXTURE, budget=2500, all_components=True)
            self.assertIn("IpcChannel", md_all)
            self.assertIn("DiagManager", md_all)
            self.assertIn("SensorHub", md_all)
            for forbidden in ("poky/", "meta-other/", "build/", "third_party/"):
                self.assertNotIn(forbidden, md_all)

            buf = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc_bad = repomap.main(["--repo", str(YOCTO_FIXTURE), "--components", "no_such_comp"])
            self.assertEqual(2, rc_bad)
            self.assertIn("ipc_bus", err.getvalue())

    def test_preflight_lint_selected_vs_all_components(self) -> None:
        """preflight_lint checks only selected component by default and finds diag_svc defect under --all-components."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVPACK_COMPONENTS", None)
            out_sel = io.StringIO()
            with contextlib.redirect_stdout(out_sel), contextlib.redirect_stderr(io.StringIO()):
                rc_sel = preflight_lint.main([
                    "--repo-root", str(YOCTO_FIXTURE),
                    "--config", str(YOCTO_FIXTURE / "preflight.toml"),
                    "--no-cache",
                    "--json",
                ])
            self.assertEqual(0, rc_sel, out_sel.getvalue())
            data_sel = json.loads(out_sel.getvalue())
            checked_sel = [r["path"] for r in data_sel["reports"]]
            self.assertTrue(any("ipc_bus" in p for p in checked_sel))
            self.assertFalse(any("diag_svc" in p or "poky" in p or "third_party" in p or "build" in p for p in checked_sel))

            out_all = io.StringIO()
            with contextlib.redirect_stdout(out_all), contextlib.redirect_stderr(io.StringIO()):
                rc_all = preflight_lint.main([
                    "--repo-root", str(YOCTO_FIXTURE),
                    "--config", str(YOCTO_FIXTURE / "preflight.toml"),
                    "--no-cache",
                    "--all-components",
                    "--json",
                ])
            self.assertEqual(1, rc_all, out_all.getvalue())
            data_all = json.loads(out_all.getvalue())
            checked_all = [r["path"] for r in data_all["reports"]]
            self.assertTrue(any("ipc_bus" in p for p in checked_all))
            self.assertTrue(any("diag_svc" in p for p in checked_all))
            self.assertTrue(any("sensor_hub" in p for p in checked_all))
            self.assertFalse(any("poky" in p or "meta-other" in p or "build" in p or "third_party" in p for p in checked_all))

    def test_cppcheck_uses_compile_commands_or_include_dirs_and_component_selection(self) -> None:
        """cppcheck uses --project= for ipc_bus, -I for diag_svc/sensor_hub, adds --suppress=missingIncludeSystem, and isolates components."""
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="",
                stderr='<?xml version="1.0" encoding="UTF-8"?>\n<results version="2"><errors/></results>\n',
            )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVPACK_COMPONENTS", None)
            with mock.patch.object(cppcheck_check.subprocess, "run", side_effect=fake_run):
                res_sel = cppcheck_check.run_cppcheck_check(
                    repo_root=YOCTO_FIXTURE,
                    config_path=YOCTO_FIXTURE / "preflight.toml",
                    cppcheck_bin=sys.executable,
                )
        self.assertEqual(0, res_sel.exit_code)
        self.assertEqual(1, len(calls))
        self.assertIn("--project=build/ipc_bus/compile_commands.json", calls[0])
        self.assertIn("--suppress=missingIncludeSystem", calls[0])

        # Real cppcheck check if installed on PATH
        if shutil.which("cppcheck") is not None:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DEVPACK_COMPONENTS", None)
                res_real_sel = cppcheck_check.run_cppcheck_check(
                    repo_root=YOCTO_FIXTURE,
                    config_path=YOCTO_FIXTURE / "preflight.toml",
                )
                self.assertEqual(0, res_real_sel.exit_code)
                self.assertEqual(0, len(res_real_sel.blocking_findings))

                res_real_all = cppcheck_check.run_cppcheck_check(
                    repo_root=YOCTO_FIXTURE,
                    config_path=YOCTO_FIXTURE / "preflight.toml",
                    all_components=True,
                )
                self.assertEqual(1, res_real_all.exit_code)
                finding_files = {f.file for f in res_real_all.blocking_findings}
                self.assertEqual(
                    {"meta-org/recipes-org/src/services/diag_svc/src/diag_svc.cpp"},
                    finding_files,
                )

    def test_brief_lint_resolves_workspace_paths_and_flags_outside_globs(self) -> None:
        """brief_lint resolves workspace-relative paths and errors on Files-allowed globs outside workspace.root."""
        ws_brief = (
            "# Brief: Update ipc_bus\n\n"
            "## Goal\n"
            "Update `publishFrame()` in `middleware/ipc_bus/src/ipc_bus.cpp`.\n\n"
            "## Input\n"
            "- `middleware/ipc_bus/include/ipc_bus.hpp:12`\n\n"
            "## Expected output\n"
            "- Keep `publishFrame()` deterministic.\n\n"
            "## Files allowed\n"
            "- `middleware/ipc_bus/**`\n\n"
            "## Constraints\n"
            "- Never modify `poky/**`.\n\n"
            "## Gate\n"
            "- `python3 enforcement/lint/preflight_lint.py --components ipc_bus`\n"
        )
        clean_findings = brief_lint.lint_brief_text(ws_brief, repo_root=YOCTO_FIXTURE, author="orchestrator")
        self.assertFalse(brief_lint.has_errors(clean_findings), clean_findings)

        bad_brief = ws_brief.replace("- `middleware/ipc_bus/**`", "- `poky/**`\n- `middleware/ipc_bus/**`")
        bad_findings = brief_lint.lint_brief_text(bad_brief, repo_root=YOCTO_FIXTURE, author="orchestrator")
        outside_errs = [f for f in bad_findings if f["rule"] == "BRIEF-SCOPE-OUTSIDE-WORKSPACE"]
        self.assertEqual(1, len(outside_errs), bad_findings)
        self.assertEqual("error", outside_errs[0]["severity"])
        self.assertIn("poky/**", outside_errs[0]["message"])

    def test_scope_check_and_dispatch_refuse_edits_outside_workspace_and_record_run_json(self) -> None:
        """scope_check and dispatch refuse edits outside workspace.root regardless of globs, and record components/scan_mode."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_from_fixture(wt)

            # Even with --allow poky/**, scope_check must refuse edits outside workspace.root
            (wt / "poky" / "meta" / "recipes-core" / "bad_poky.c").write_text("int x = 1;\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc_sc = scope_check.main([
                    "--repo", str(wt),
                    "--base", "HEAD",
                    "--allow", "poky/**",
                ])
            self.assertEqual(1, rc_sc)

            # Revert poky edit for clean dispatch test
            subprocess.run(["git", "checkout", "--", "poky"], cwd=wt, check=True, capture_output=True)

            brief = base / "brief.md"
            brief.write_text("# Brief\nUpdate file.\n", encoding="utf-8")
            runs_root = base / "runs"

            # 1. Dispatch where agent edits poky/ despite --allow poky/** -> refused as scope violation
            bad_agent = _write_fake_agent(
                base / "bad_agent.py",
                "pathlib.Path('poky/meta/recipes-core/bad_poky.c').write_text('int x = 2;\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'result': 'done'}))\n",
            )
            out1 = io.StringIO()
            with contextlib.redirect_stdout(out1), contextlib.redirect_stderr(io.StringIO()):
                rc1 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "poky/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--agent-bin", str(bad_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ])
            self.assertEqual(1, rc1)
            payload1 = json.loads(out1.getvalue().strip())
            self.assertEqual("violation", payload1["scope"])
            run1_json = json.loads((Path(payload1["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(["ipc_bus"], run1_json["components"])
            self.assertEqual("selected", run1_json["scan_mode"])

            # Clean poky edit before next run
            subprocess.run(["git", "checkout", "--", "poky"], cwd=wt, check=True, capture_output=True)

            # 2. Dispatch where agent edits active component ipc_bus cleanly with --components ipc_bus
            good_agent = _write_fake_agent(
                base / "good_agent.py",
                "p = pathlib.Path('meta-org/recipes-org/src/middleware/ipc_bus/src/ipc_bus.cpp')\n"
                "p.write_text(p.read_text(encoding='utf-8').replace('0U', '1U'), encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'result': 'done'}))\n",
            )
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(io.StringIO()):
                rc2 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "meta-org/recipes-org/src/middleware/ipc_bus/**",
                    "--components", "ipc_bus",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--agent-bin", str(good_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ])
            self.assertEqual(0, rc2, out2.getvalue())
            payload2 = json.loads(out2.getvalue().strip())
            self.assertEqual("ok", payload2["gate"])
            self.assertEqual("ok", payload2["scope"])
            self.assertEqual(["ipc_bus"], payload2["components"])
            self.assertEqual("selected", payload2["scan_mode"])

    def test_gate_and_devpack_run_component_flags(self) -> None:
        """gate.py and devpack_run.py accept --components/--all-components and fail closed on unknown components."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            gate_cfg = base / "gate.toml"
            gate_cfg.write_text(
                '[[check]]\nname = "echo-env"\n'
                'cmd = ["python3", "-c", "import os, sys; sys.exit(0 if os.environ.get(\'DEVPACK_COMPONENTS\') == \'diag_svc\' else 1)"]\n'
                "timeout_s = 10\nrequired = true\n",
                encoding="utf-8",
            )
            receipt = base / "receipt.json"
            with contextlib.redirect_stdout(io.StringIO()):
                rc_ok = gate.main([
                    "--config", str(gate_cfg),
                    "--repo", str(YOCTO_FIXTURE),
                    "--receipt", str(receipt),
                    "--components", "diag_svc",
                ])
            self.assertEqual(0, rc_ok)
            rc_data = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(["diag_svc"], rc_data["components"])
            self.assertEqual("selected", rc_data["scan_mode"])

            with contextlib.redirect_stdout(io.StringIO()):
                rc_bad = gate.main([
                    "--config", str(gate_cfg),
                    "--repo", str(YOCTO_FIXTURE),
                    "--receipt", str(receipt),
                    "--components", "unknown_xyz",
                ])
            self.assertEqual(1, rc_bad)

            # devpack_run --dry-run with --all-components and unknown --components
            with contextlib.redirect_stdout(io.StringIO()):
                rc_run_ok = devpack_run.main([
                    "--worktree", str(YOCTO_FIXTURE),
                    "--all-components",
                    "--dry-run",
                ])
            self.assertEqual(0, rc_run_ok)
            with contextlib.redirect_stderr(io.StringIO()):
                rc_run_bad = devpack_run.main([
                    "--worktree", str(YOCTO_FIXTURE),
                    "--components", "nonexistent",
                    "--dry-run",
                ])
            self.assertEqual(2, rc_run_bad)

    def test_workspace_scope_cli_subcommands_and_310_toml_fallback(self) -> None:
        """workspace_scope CLI list/resolve/filter and 3.10 TOML array-of-tables fallback work."""
        out_list = io.StringIO()
        with contextlib.redirect_stdout(out_list):
            rc_list = workspace_scope.main(["list", "--repo", str(YOCTO_FIXTURE), "--json"])
        self.assertEqual(0, rc_list)
        listed = json.loads(out_list.getvalue())
        self.assertEqual(["ipc_bus", "diag_svc", "sensor_hub"], [c["name"] for c in listed["components"]])

        out_res = io.StringIO()
        with contextlib.redirect_stdout(out_res):
            rc_res = workspace_scope.main(["resolve", "--repo", str(YOCTO_FIXTURE), "--components", "diag_svc"])
        self.assertEqual(0, rc_res)
        self.assertEqual(["meta-org/recipes-org/src/services/diag_svc/**"], out_res.getvalue().strip().splitlines())

        out_flt = io.StringIO()
        with contextlib.redirect_stdout(out_flt):
            rc_flt = workspace_scope.main([
                "filter",
                "--repo", str(YOCTO_FIXTURE),
                "--all-components",
                "poky/bad.c",
                "meta-org/recipes-org/src/middleware/ipc_bus/third_party/vendor_stub.cpp",
                "meta-org/recipes-org/src/apps/sensor_hub/src/sensor_hub.cpp",
            ])
        self.assertEqual(0, rc_flt)
        self.assertEqual(
            ["meta-org/recipes-org/src/apps/sensor_hub/src/sensor_hub.cpp"],
            out_flt.getvalue().strip().splitlines(),
        )

        # Verify Python 3.10 fallback TOML parsers in workspace_scope and agent_exec
        toml_text = (YOCTO_FIXTURE / ".devpack" / "project.toml").read_text(encoding="utf-8")
        with mock.patch.object(workspace_scope, "tomllib", None):
            parsed_ws = workspace_scope.parse_toml_with_arrays(toml_text)
        self.assertEqual("meta-org/recipes-org/src", parsed_ws["workspace"]["root"])
        self.assertEqual(3, len(parsed_ws["component"]))
        self.assertEqual("ipc_bus", parsed_ws["component"][0]["name"])
        self.assertEqual(["ipc_bus"], parsed_ws["scan"]["selected"])

        parsed_ae = agent_exec._minimal_toml_loads(toml_text)
        self.assertEqual(3, len(parsed_ae["component"]))
        self.assertEqual("diag_svc", parsed_ae["component"][1]["name"])

    def test_devpack_run_records_components_and_scan_mode_in_run_json(self) -> None:
        """devpack_run full execution records components and scan_mode in run.json."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_from_fixture(wt)
            script = base / "driver.py"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "role = sys.argv[1]\n"
                "if role == 'implementer':\n"
                "    p = pathlib.Path('meta-org/recipes-org/src/apps/sensor_hub/src/sensor_hub.cpp')\n"
                "    p.write_text(p.read_text(encoding='utf-8').replace('1U', '2U'), encoding='utf-8')\n"
                "    print('implemented')\n"
                "elif role == 'reviewer':\n"
                "    print('VERDICT: APPROVE')\n",
                encoding="utf-8",
            )
            os.chmod(script, stat.S_IRWXU)
            engines_toml = base / "engines.toml"
            engines_toml.write_text(
                f'gate_cmd = ["python3", "-c", "import sys; sys.exit(0)"]\n'
                "[roles.implementer]\n"
                'model = "sonnet"\n'
                f'cmd = ["python3", "{script}", "implementer", "{{prompt}}"]\n'
                "[roles.reviewer]\n"
                'model = "sonnet"\n'
                f'cmd = ["python3", "{script}", "reviewer", "{{prompt}}"]\n',
                encoding="utf-8",
            )
            runs_root = base / "runs"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = devpack_run.main([
                    "--config", str(engines_toml),
                    "--worktree", str(wt),
                    "--runs-root", str(runs_root),
                    "--components", "sensor_hub",
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ])
            self.assertEqual(0, rc)
            run_dirs = [d for d in runs_root.iterdir() if d.is_dir()]
            self.assertEqual(1, len(run_dirs))
            rj = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(["sensor_hub"], rj["components"])
            self.assertEqual("selected", rj["scan_mode"])


if __name__ == "__main__":
    unittest.main()
