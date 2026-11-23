"""Unit tests for runner/agent_exec.py covering v2/v0.1 configs, isolation, streaming, and classification."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_exec  # noqa: E402
from agent_exec import (  # noqa: E402
    AgentRequest,
    EngineConfig,
    build_invocation,
    load_engines,
    new_run_dir,
    parse_stream,
    redact_sensitive_text,
    run_agent,
)


def _write_executable(path: Path, content: str) -> Path:
    """Write an executable Python script to ``path``."""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class AgentExecUnitTests(unittest.TestCase):
    """Test suite for v2/v0.1 engine configs, local env, stream parsing, redaction, and classification."""

    def test_v2_and_v01_config_parsing(self) -> None:
        """Verify v2 engines.example.toml loads cleanly and v0.1 files load with a stderr DeprecationWarning."""
        example_toml = RUNNER_DIR / "engines.example.toml"
        cfg = load_engines(example_toml)
        self.assertEqual(2, cfg.version)
        self.assertIn("sonnet", cfg.engines)
        self.assertIn("haiku", cfg.engines)
        self.assertIn("local", cfg.engines)

        sonnet = cfg.engines["sonnet"]
        self.assertEqual("sonnet", sonnet.model)
        self.assertEqual("cloud", sonnet.engine_class)
        self.assertEqual("internal", sonnet.max_data_class)

        local = cfg.engines["local"]
        self.assertEqual("local-coder", local.model)
        self.assertEqual("local", local.engine_class)
        self.assertEqual("confidential", local.max_data_class)
        self.assertEqual("DEVPACK_LOCAL_BASE_URL", local.base_url_env)
        self.assertEqual("DEVPACK_LOCAL_AUTH_TOKEN", local.auth_token_env)
        self.assertEqual(32768, local.min_context_tokens)

        self.assertEqual("haiku", cfg.roles["implementer"])
        self.assertEqual("sonnet", cfg.roles["reviewer"])
        self.assertEqual("haiku", cfg.roles["fixer"])
        self.assertEqual("sonnet", cfg.roles["orchestrator"])
        self.assertEqual(2, cfg.limits["max_fix_rounds"])
        self.assertEqual(2, cfg.max_fix_rounds)

        with tempfile.TemporaryDirectory() as tmp:
            v01_path = Path(tmp) / "v01_engines.toml"
            v01_path.write_text(
                "max_fix_rounds = 2\n"
                'gate_cmd = ["python3", "gate.py"]\n'
                "[roles.implementer]\n"
                'model = "haiku"\n'
                'cmd = ["claude", "-p", "--model", "haiku", "{prompt}"]\n'
                "timeout_s = 120\n"
                "[roles.reviewer]\n"
                'model = "sonnet"\n'
                'cmd = ["claude", "-p", "--model", "sonnet", "{prompt}"]\n'
                "timeout_s = 180\n",
                encoding="utf-8",
            )
            stderr_buf = io.StringIO()
            with mock.patch("sys.stderr", stderr_buf):
                v01_cfg = load_engines(v01_path)
            stderr_out = stderr_buf.getvalue()
            self.assertIn("DeprecationWarning", stderr_out)
            self.assertEqual(1, v01_cfg.version)
            self.assertEqual("haiku", v01_cfg.engine_for_role("implementer").model)
            self.assertEqual("sonnet", v01_cfg.engine_for_role("reviewer").model)
            self.assertEqual(120, v01_cfg.engine_for_role("implementer").timeout_s)

            # Reject literal URL in base_url_env
            bad_v2 = Path(tmp) / "bad_v2.toml"
            bad_v2.write_text(
                "version = 2\n"
                "[engines.local]\n"
                'model = "m"\n'
                'class = "local"\n'
                'base_url_env = "http://localhost:11434"\n'
                'auth_token_env = "TOK"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_engines(bad_v2)

    def test_local_engine_env_and_token_redaction(self) -> None:
        """Verify local engine sets base URL/token/model env vars, removes ANTHROPIC_API_KEY, and never logs token."""
        local_eng = EngineConfig(
            name="local",
            model="local-coder-7b",
            engine_class="local",
            max_data_class="confidential",
            base_url_env="TEST_LOCAL_BASE_URL",
            auth_token_env="TEST_LOCAL_AUTH_TOKEN",
            min_context_tokens=32768,
        )
        secret_tok = "LOCAL_BEARER_SECRET_TOKEN_XYZ987"
        base_url = "http://127.0.0.1:11434"

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            workdir.mkdir()
            run_dir = new_run_dir(base / "runs", kind="task", task="t-local", engines=["local"])

            # Missing env var -> harness_error with clear message
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TEST_LOCAL_BASE_URL", None)
                os.environ.pop("TEST_LOCAL_AUTH_TOKEN", None)
                res_missing = run_agent(
                    AgentRequest(
                        engine=local_eng,
                        prompt="Hello local",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="01-implementer-local",
                        isolation="local",
                        allow_unsandboxed_local=True,
                    )
                )
                self.assertEqual("harness_error", res_missing.status)
                self.assertIn("TEST_LOCAL_BASE_URL", res_missing.error)

            env_dump_path = base / "local_env_dump.json"
            fake_claude = _write_executable(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "cfg_dir = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
                "dump = {\n"
                "    'ANTHROPIC_BASE_URL': os.environ.get('ANTHROPIC_BASE_URL'),\n"
                "    'ANTHROPIC_AUTH_TOKEN': os.environ.get('ANTHROPIC_AUTH_TOKEN'),\n"
                "    'ANTHROPIC_MODEL': os.environ.get('ANTHROPIC_MODEL'),\n"
                "    'ANTHROPIC_DEFAULT_HAIKU_MODEL': os.environ.get('ANTHROPIC_DEFAULT_HAIKU_MODEL'),\n"
                "    'ANTHROPIC_DEFAULT_SONNET_MODEL': os.environ.get('ANTHROPIC_DEFAULT_SONNET_MODEL'),\n"
                "    'has_api_key': 'ANTHROPIC_API_KEY' in os.environ,\n"
                "    'has_creds_file': (cfg_dir / '.credentials.json').exists(),\n"
                "}\n"
                f"pathlib.Path({str(env_dump_path)!r}).write_text(json.dumps(dump), encoding='utf-8')\n"
                "sys.stderr.write('[claude-code:unrecognized_model] model=local-coder-7b\\n')\n"
                f"sys.stderr.write('leaked token: {secret_tok}\\n')\n"
                "print(json.dumps({'type': 'assistant', 'message': {'model': 'local-coder-7b', 'content': [{'type': 'text', 'text': 'Echo ' + "
                + repr(secret_tok)
                + "}]}}))\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.0, 'num_turns': 1, 'duration_ms': 250, 'usage': {'input_tokens': 12, 'output_tokens': 5}, 'result': 'Done ' + "
                + repr(secret_tok)
                + "}))\n",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "TEST_LOCAL_BASE_URL": base_url,
                    "TEST_LOCAL_AUTH_TOKEN": secret_tok,
                    "ANTHROPIC_API_KEY": "CLOUD_API_KEY_SHOULD_BE_REMOVED",
                },
                clear=False,
            ):
                argv, built_env = build_invocation(
                    local_eng,
                    prompt="Test prompt",
                    workdir=workdir,
                    agent_bin=str(fake_claude),
                )
                self.assertEqual(base_url, built_env["ANTHROPIC_BASE_URL"])
                self.assertEqual(secret_tok, built_env["ANTHROPIC_AUTH_TOKEN"])
                self.assertEqual("local-coder-7b", built_env["ANTHROPIC_MODEL"])
                self.assertEqual("local-coder-7b", built_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"])
                self.assertEqual("local-coder-7b", built_env["ANTHROPIC_DEFAULT_SONNET_MODEL"])
                self.assertNotIn("ANTHROPIC_API_KEY", built_env)
                self.assertNotIn(secret_tok, " ".join(argv))

                res = run_agent(
                    AgentRequest(
                        engine=local_eng,
                        prompt=f"Prompt mentioning {secret_tok}",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="02-implementer-local",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin=str(fake_claude),
                    )
                )

            # [claude-code:unrecognized_model] on stderr is a benign warning, so status is ok
            self.assertEqual("ok", res.status)
            dump = json.loads(env_dump_path.read_text(encoding="utf-8"))
            self.assertEqual(base_url, dump["ANTHROPIC_BASE_URL"])
            self.assertEqual(secret_tok, dump["ANTHROPIC_AUTH_TOKEN"])
            self.assertEqual("local-coder-7b", dump["ANTHROPIC_MODEL"])
            self.assertEqual("local-coder-7b", dump["ANTHROPIC_DEFAULT_HAIKU_MODEL"])
            self.assertEqual("local-coder-7b", dump["ANTHROPIC_DEFAULT_SONNET_MODEL"])
            self.assertFalse(dump["has_api_key"])
            self.assertFalse(dump["has_creds_file"])

            # Verify token is absent from all written files in run_dir
            written_files = list(run_dir.rglob("*"))
            self.assertGreaterEqual(len([p for p in written_files if p.is_file()]), 4)
            for file_path in written_files:
                if file_path.is_file():
                    content = file_path.read_text(encoding="utf-8")
                    self.assertNotIn(secret_tok, content, f"Token leaked in {file_path}")
            self.assertNotIn(secret_tok, res.final_text)
            self.assertNotIn(secret_tok, res.stderr)

    def test_disable_slash_commands_present_iff_skills_disabled(self) -> None:
        """Verify --disable-slash-commands is included when skills_enabled=False and omitted when True."""
        eng = EngineConfig(name="haiku", model="haiku", engine_class="cloud")
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()

            argv_disabled, _ = build_invocation(
                eng, prompt="run", workdir=workdir, skills_enabled=False
            )
            self.assertIn("--disable-slash-commands", argv_disabled)
            self.assertIn("--output-format", argv_disabled)
            fmt_idx = argv_disabled.index("--output-format")
            self.assertEqual("stream-json", argv_disabled[fmt_idx + 1])
            self.assertIn("--verbose", argv_disabled)

            argv_enabled, _ = build_invocation(
                eng, prompt="run", workdir=workdir, skills_enabled=True
            )
            self.assertNotIn("--disable-slash-commands", argv_enabled)

            docker_disabled, _ = build_invocation(
                eng,
                prompt="run",
                workdir=workdir,
                isolation="docker",
                skills_enabled=False,
            )
            self.assertIn("--disable-slash-commands", docker_disabled)

            docker_enabled, _ = build_invocation(
                eng,
                prompt="run",
                workdir=workdir,
                isolation="docker",
                skills_enabled=True,
            )
            self.assertNotIn("--disable-slash-commands", docker_enabled)

    def test_stream_parsing_of_both_recorded_samples(self) -> None:
        """Verify parse_stream extracts cost, turns, tool counts, and tool_protocol_errors on both samples."""
        haiku_sample = FIXTURES_DIR / "stream_haiku_toolcalls.jsonl"
        local_sample = FIXTURES_DIR / "stream_local_tool_protocol_error.jsonl"
        self.assertTrue(haiku_sample.is_file())
        self.assertTrue(local_sample.is_file())

        haiku_res = parse_stream(haiku_sample)
        self.assertEqual("ok", haiku_res.status)
        self.assertAlmostEqual(0.00221478, haiku_res.cost_usd, places=7)
        self.assertEqual(4, haiku_res.num_turns)
        self.assertEqual({"Read": 1, "Edit": 1, "Bash": 1}, haiku_res.tool_calls)
        self.assertEqual(0, haiku_res.tool_protocol_errors)
        self.assertEqual(6, haiku_res.tokens_in)
        self.assertEqual(578, haiku_res.tokens_out)
        self.assertIn("return a+b;", haiku_res.final_text)

        local_res = parse_stream(local_sample)
        self.assertEqual("ok", local_res.status)
        self.assertAlmostEqual(0.01526, local_res.cost_usd, places=5)
        self.assertEqual(1, local_res.num_turns)
        self.assertEqual({}, local_res.tool_calls)
        self.assertGreaterEqual(local_res.tool_protocol_errors, 1)
        self.assertEqual(2050, local_res.tokens_in)
        self.assertEqual(353, local_res.tokens_out)
        self.assertIn('"name": "Run"', local_res.final_text)

    def test_redaction_and_credentials_cleanup_after_call(self) -> None:
        """Verify fake credentials and fake home paths are redacted and credentials copy is deleted after call."""
        fake_cred_secret = "FAKE_OAUTH_CREDENTIAL_SECRET_445566"
        fake_home = "/" + "ho" + "me/fake_developer_99/workspace/file.cpp"

        redacted = redact_sensitive_text(
            f"Token={fake_cred_secret} at {fake_home}",
            extra_secrets=[fake_cred_secret],
        )
        self.assertNotIn(fake_cred_secret, redacted)
        self.assertNotIn("/" + "ho" + "me/", redacted)
        self.assertNotIn("fake_developer_99", redacted)
        self.assertIn("~/workspace/file.cpp", redacted)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            workdir.mkdir()
            cfg_dir = base / "custom_cfg"
            home_dir = base / "custom_home"
            tmp_dir = base / "custom_tmp"
            cred_file = base / "creds.json"
            cred_file.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": fake_cred_secret}}),
                encoding="utf-8",
            )

            seen_during_run = base / "seen_during_run.json"
            fake_claude = _write_executable(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "cfg = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
                "state = json.loads((cfg / '.claude.json').read_text(encoding='utf-8'))\n"
                "has_creds = (cfg / '.credentials.json').is_file()\n"
                f"pathlib.Path({str(seen_during_run)!r}).write_text(json.dumps({{'state': state, 'has_creds': has_creds}}), encoding='utf-8')\n"
                f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'duration_ms': 100, 'usage': {{'input_tokens': 10, 'output_tokens': 4}}, 'result': 'Used {fake_cred_secret} in {fake_home}'}}))\n",
            )

            run_dir = new_run_dir(base / "runs", kind="task", task="t-redact", engines=["haiku"])
            res = run_agent(
                AgentRequest(
                    engine=EngineConfig(name="haiku", model="haiku", engine_class="cloud"),
                    prompt=f"Inspect {fake_home}",
                    workdir=workdir,
                    run_dir=run_dir,
                    step_name="01-implementer-haiku",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    credentials_path=cred_file,
                    agent_bin=str(fake_claude),
                    home_dir=home_dir,
                    cfg_dir=cfg_dir,
                    tmp_dir=tmp_dir,
                )
            )
            self.assertEqual("ok", res.status)

            # During the run, credentials and onboarding/trust state were present
            seen = json.loads(seen_during_run.read_text(encoding="utf-8"))
            self.assertTrue(seen["has_creds"])
            self.assertTrue(seen["state"]["hasCompletedOnboarding"])
            self.assertTrue(
                seen["state"]["projects"][str(workdir.resolve())]["hasTrustDialogAccepted"]
            )

            # After the run, credentials copy and cfg_dir contents are removed
            self.assertFalse((cfg_dir / ".credentials.json").exists())
            self.assertEqual([], list(cfg_dir.iterdir()))

            # Check jsonl, meta.json, prompt.md exist and contain no secret or home path
            jsonl_path = run_dir / "steps" / "01-implementer-haiku.jsonl"
            meta_path = run_dir / "steps" / "01-implementer-haiku.meta.json"
            prompt_path = run_dir / "steps" / "01-implementer-haiku.prompt.md"
            for artifact in (jsonl_path, meta_path, prompt_path):
                self.assertTrue(artifact.is_file())
                text = artifact.read_text(encoding="utf-8")
                self.assertNotIn(fake_cred_secret, text)
                self.assertNotIn(fake_home, text)
                self.assertNotIn("/" + "ho" + "me/", text)

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(64, len(meta["prompt_sha256"]))
            self.assertEqual("01-implementer-haiku.prompt.md", meta["prompt_file"])

    def test_timeout_and_missing_result_classification_via_fake_claude_on_path(self) -> None:
        """Verify timeout, missing-result, auth failure, and agent_error classification using fake claude on PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            workdir = base / "work"
            workdir.mkdir()
            run_dir = new_run_dir(base / "runs", kind="task", task="t-classify", engines=["haiku"])

            fake_claude = _write_executable(
                bin_dir / "claude",
                "#!/usr/bin/env python3\n"
                "import json, os, sys, time\n"
                "mode = os.environ.get('FAKE_CLAUDE_MODE', 'missing_result')\n"
                "prompt = sys.argv[-1]\n"
                "if 'MODE_TIMEOUT' in prompt:\n"
                "    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'haiku'}), flush=True)\n"
                "    time.sleep(5)\n"
                "elif 'MODE_MISSING_RESULT' in prompt:\n"
                "    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'haiku'}))\n"
                "    print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial output without result'}]}}))\n"
                "elif 'MODE_AUTH_FAIL' in prompt:\n"
                "    sys.stderr.write('Error: authentication_error: invalid api key\\n')\n"
                "    sys.exit(1)\n"
                "elif 'MODE_AGENT_ERROR' in prompt:\n"
                "    print(json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': 'max turns exceeded', 'num_turns': 5, 'total_cost_usd': 0.01}))\n"
                "    sys.exit(1)\n",
            )
            self.assertTrue(fake_claude.is_file())

            patched_path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
            eng = EngineConfig(name="haiku", model="haiku", engine_class="cloud", timeout_s=1)

            with mock.patch.dict(os.environ, {"PATH": patched_path}, clear=False):
                res_timeout = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="Run MODE_TIMEOUT",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="01-implementer-haiku",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin="claude",
                        timeout_s=1,
                    )
                )
                self.assertEqual("timeout", res_timeout.status)
                self.assertEqual(124, res_timeout.exit_code)
                self.assertIn("timed out", res_timeout.error)

                res_no_result = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="Run MODE_MISSING_RESULT",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="02-implementer-haiku",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin="claude",
                        timeout_s=5,
                    )
                )
                self.assertEqual("harness_error", res_no_result.status)
                self.assertIn("no result event", res_no_result.error)

                res_auth = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="Run MODE_AUTH_FAIL",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="03-implementer-haiku",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin="claude",
                        timeout_s=5,
                    )
                )
                self.assertEqual("harness_error", res_auth.status)
                self.assertIn("authentication", res_auth.error)

                res_agent_err = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="Run MODE_AGENT_ERROR",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="04-implementer-haiku",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin="claude",
                        timeout_s=5,
                    )
                )
                self.assertEqual("agent_error", res_agent_err.status)
                self.assertEqual(5, res_agent_err.num_turns)

    def test_sandbox_local_isolation_refused_without_explicit_allow_and_warns_when_allowed(self) -> None:
        """Verify AgentRequest defaults to docker and isolation='local' refuses unless explicitly authorized."""
        default_req = AgentRequest(engine="haiku", prompt="hi")
        self.assertEqual("docker", default_req.isolation)
        self.assertFalse(default_req.allow_unsandboxed_local)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            workdir.mkdir()
            marker = base / "invoked.txt"
            fake_claude = _write_executable(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.0, 'num_turns': 1, 'result': 'ok'}))\n",
            )
            eng = EngineConfig(name="haiku", model="haiku", engine_class="cloud")

            # 1. Refused without allow_unsandboxed_local or project.toml override
            run_refused = new_run_dir(base / "runs", kind="task", task="refuse", engines=["haiku"])
            stderr_buf = io.StringIO()
            with mock.patch("sys.stderr", stderr_buf):
                res_refused = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="test",
                        workdir=workdir,
                        run_dir=run_refused,
                        isolation="local",
                        agent_bin=str(fake_claude),
                    )
                )
            self.assertEqual("harness_error", res_refused.status)
            self.assertIn("--allow-unsandboxed-local", res_refused.error)
            self.assertFalse(marker.exists())

            # 2. Allowed via explicit flag -> prints warning and records in run.json
            run_flag = new_run_dir(base / "runs", kind="task", task="allow-flag", engines=["haiku"])
            stderr_buf2 = io.StringIO()
            with mock.patch("sys.stderr", stderr_buf2):
                res_flag = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="test",
                        workdir=workdir,
                        run_dir=run_flag,
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin=str(fake_claude),
                    )
                )
            self.assertEqual("ok", res_flag.status)
            self.assertTrue(marker.exists())
            self.assertIn("WARNING:", stderr_buf2.getvalue())
            rj = json.loads((run_flag / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("local", rj["isolation"])
            self.assertIn("WARNING:", rj["sandbox_warning"])

            # 3. Workdir .devpack/project.toml allow_unsandboxed_local = true MUST BE IGNORED
            #    with a warning recorded in run.json.
            (workdir / ".devpack").mkdir()
            (workdir / ".devpack" / "project.toml").write_text(
                'data_class = "internal"\nallow_unsandboxed_local = true\n',
                encoding="utf-8",
            )
            run_proj = new_run_dir(base / "runs", kind="task", task="ignore-proj", engines=["haiku"])
            stderr_buf3 = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DEVPACK_OPERATOR_CONFIG", None)
                with mock.patch("sys.stderr", stderr_buf3):
                    res_proj = run_agent(
                        AgentRequest(
                            engine=eng,
                            prompt="test",
                            workdir=workdir,
                            run_dir=run_proj,
                            isolation="local",
                            agent_bin=str(fake_claude),
                        )
                    )
            self.assertEqual("harness_error", res_proj.status)
            self.assertIn("ignoring allow_unsandboxed_local", stderr_buf3.getvalue())
            rj_proj = json.loads((run_proj / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("refused", rj_proj["outcome"])
            self.assertTrue(rj_proj["ignored_workdir_sandbox_opt_out"])
            self.assertIn("ignoring allow_unsandboxed_local", rj_proj["sandbox_warning"])

            # 4. Operator-owned DEVPACK_OPERATOR_CONFIG outside workdir IS honoured.
            op_cfg = base / "operator.toml"
            op_cfg.write_text("allow_unsandboxed_local = true\n", encoding="utf-8")
            run_op = new_run_dir(base / "runs", kind="task", task="allow-op", engines=["haiku"])
            stderr_buf4 = io.StringIO()
            with mock.patch.dict(os.environ, {"DEVPACK_OPERATOR_CONFIG": str(op_cfg)}, clear=False):
                with mock.patch("sys.stderr", stderr_buf4):
                    res_op = run_agent(
                        AgentRequest(
                            engine=eng,
                            prompt="test",
                            workdir=workdir,
                            run_dir=run_op,
                            isolation="local",
                            agent_bin=str(fake_claude),
                        )
                    )
            self.assertEqual("ok", res_op.status)
            rj_op = json.loads((run_op / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("local", rj_op["isolation"])
            self.assertIn("WARNING:", rj_op["sandbox_warning"])

            # 5. DEVPACK_OPERATOR_CONFIG placed inside workdir is refused (fail-closed).
            evil_op = workdir / "operator_inside.toml"
            evil_op.write_text("allow_unsandboxed_local = true\n", encoding="utf-8")
            run_evil_op = new_run_dir(base / "runs", kind="task", task="refuse-evil-op", engines=["haiku"])
            with mock.patch.dict(os.environ, {"DEVPACK_OPERATOR_CONFIG": str(evil_op)}, clear=False):
                res_evil_op = run_agent(
                    AgentRequest(
                        engine=eng,
                        prompt="test",
                        workdir=workdir,
                        run_dir=run_evil_op,
                        isolation="local",
                        agent_bin=str(fake_claude),
                    )
                )
            self.assertEqual("harness_error", res_evil_op.status)
            self.assertIn("must be outside workdir", res_evil_op.error)

    def test_docker_mounts_dot_git_read_only_for_every_role_and_worktrees(self) -> None:
        """Container mount list must include .git:ro for every role, plus worktree gitdir:ro."""
        eng = EngineConfig(name="haiku", model="haiku", engine_class="cloud")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            for args in (
                ["init", "-q"],
                ["config", "user.email", "t@example.invalid"],
                ["config", "user.name", "Tester"],
                ["commit", "-q", "--allow-empty", "-m", "init"],
            ):
                subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

            for role in ("implementer", "reviewer", "fixer", "orchestrator", "broker-worker", "eval"):
                req = AgentRequest(
                    engine=eng,
                    prompt="check mounts",
                    workdir=repo,
                    run_dir=base / "runs" / role,
                    isolation="docker",
                    role=role,
                )
                argv, _ = build_invocation(req)
                mounts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]
                self.assertIn(f"{repo / '.git'}:/workspace/task/.git:ro", mounts, f"role={role}")

            # Worktree: .git is a pointer file; both .git and the external gitdir must be mounted :ro
            wt = base / "wt"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "wt-ro", str(wt)],
                check=True,
                capture_output=True,
            )
            wt_gitdir = (repo / ".git" / "worktrees" / "wt").resolve()
            for role in ("implementer", "reviewer", "orchestrator"):
                req_wt = AgentRequest(
                    engine=eng,
                    prompt="check worktree mounts",
                    workdir=wt,
                    run_dir=base / "runs" / f"wt-{role}",
                    isolation="docker",
                    role=role,
                )
                argv_wt, _ = build_invocation(req_wt)
                mounts_wt = [argv_wt[i + 1] for i, tok in enumerate(argv_wt) if tok == "-v"]
                self.assertIn(f"{wt / '.git'}:/workspace/task/.git:ro", mounts_wt, f"role={role}")
                self.assertIn(f"{wt_gitdir}:{wt_gitdir}:ro", mounts_wt, f"role={role}")

    def test_profile_injection_meta_hash_and_hot_reload_from_disk(self) -> None:
        """Verify profile_path injects under '## Known mistakes for this engine', records sha256+version in meta, and reloads per call."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            workdir.mkdir()
            profile = base / "local.md"
            profile.write_text(
                "# Engine Profile: local\nVersion: 1\n"
                "- [DOXYGEN-PARAM-DIR] (4 occurrences): Missing [in] -> Always write @param[in].\n",
                encoding="utf-8",
            )
            fake_claude = _write_executable(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'qwen3-30b'}))\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': 'ok'}))\n",
            )
            eng = EngineConfig(name="haiku", model="haiku", engine_class="cloud")
            run_dir = new_run_dir(base / "runs", kind="task", task="t-prof", engines=["haiku"])

            res1 = run_agent(
                AgentRequest(
                    engine=eng,
                    prompt="Implement feature A.",
                    workdir=workdir,
                    run_dir=run_dir,
                    step_name="01-implementer-haiku",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(fake_claude),
                    profile_path=profile,
                )
            )
            self.assertEqual("ok", res1.status)
            self.assertEqual("qwen3-30b", res1.init_model)
            self.assertEqual(1, res1.profile_version)
            self.assertEqual(64, len(res1.profile_sha256))
            prompt1 = (run_dir / "steps" / "01-implementer-haiku.prompt.md").read_text(encoding="utf-8")
            self.assertIn("## Known mistakes for this engine", prompt1)
            self.assertIn("DOXYGEN-PARAM-DIR", prompt1)
            meta1 = json.loads((run_dir / "steps" / "01-implementer-haiku.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(res1.profile_sha256, meta1["profile_sha256"])
            self.assertEqual(1, meta1["profile_version"])

            # Edit profile on disk between calls -> next call must reload new content, version, and hash
            profile.write_text(
                "# Engine Profile: local\nVersion: 2\n"
                "- [NO-IMPL-IN-HEADER] (6 occurrences): inline in header -> Define in .cpp only.\n",
                encoding="utf-8",
            )
            res2 = run_agent(
                AgentRequest(
                    engine=eng,
                    prompt="Implement feature B.",
                    workdir=workdir,
                    run_dir=run_dir,
                    step_name="02-implementer-haiku",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(fake_claude),
                    profile_path=profile,
                )
            )
            self.assertEqual(2, res2.profile_version)
            self.assertNotEqual(res1.profile_sha256, res2.profile_sha256)
            prompt2 = (run_dir / "steps" / "02-implementer-haiku.prompt.md").read_text(encoding="utf-8")
            self.assertIn("NO-IMPL-IN-HEADER", prompt2)
            self.assertNotIn("DOXYGEN-PARAM-DIR", prompt2)
            meta2 = json.loads((run_dir / "steps" / "02-implementer-haiku.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(res2.profile_sha256, meta2["profile_sha256"])
            self.assertEqual(2, meta2["profile_version"])

            # Empty Version 0 stub records sha256 + version=0 without injecting empty section
            stub = REPO_ROOT / "knowledge" / "engine_profiles" / "local.md"
            res3 = run_agent(
                AgentRequest(
                    engine=eng,
                    prompt="Implement feature C.",
                    workdir=workdir,
                    run_dir=run_dir,
                    step_name="03-implementer-haiku",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(fake_claude),
                    profile_path=stub,
                )
            )
            self.assertEqual(0, res3.profile_version)
            self.assertEqual(64, len(res3.profile_sha256))
            prompt3 = (run_dir / "steps" / "03-implementer-haiku.prompt.md").read_text(encoding="utf-8")
            self.assertNotIn("## Known mistakes for this engine", prompt3)


if __name__ == "__main__":
    unittest.main()
