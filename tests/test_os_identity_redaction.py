# Header: Unit tests for OS user, group, and host name redaction in agent_exec and devpack_logs.
"""Unit tests verifying OS user, group, and host redaction in runner logs and streams."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIR = REPO_ROOT / "runner"
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import agent_exec  # noqa: E402
from runner import devpack_logs  # noqa: E402


class _FakePwEntry:
    """Minimal pwd.struct_passwd substitute."""

    def __init__(self, pw_name: str) -> None:
        """Store fake username."""
        self.pw_name = pw_name


class _FakeGroupEntry:
    """Minimal grp.struct_group substitute."""

    def __init__(self, gr_name: str) -> None:
        """Store fake group name."""
        self.gr_name = gr_name


def _mock_os_context(
    user: str = "jdoe_eng",
    primary_group: str = "eng_team",
    extra_groups: dict[int, str] | None = None,
    hostname: str = "workstation-42.corp.internal",
):
    """Return a context manager mocking OS user, group, and host lookups."""
    groups_map: dict[int, str] = {1000: primary_group}
    if extra_groups:
        groups_map.update(extra_groups)

    def _getpwuid(_uid: int) -> _FakePwEntry:
        """Return fake pw entry for current user."""
        return _FakePwEntry(user)

    def _getgrgid(gid: int) -> _FakeGroupEntry:
        """Return fake group entry for ``gid``."""
        if gid in groups_map:
            return _FakeGroupEntry(groups_map[gid])
        raise KeyError(gid)

    class _Ctx:
        """Combined mock context manager."""

        def __enter__(self) -> None:
            """Activate all OS identity patches."""
            self._patches = [
                mock.patch.dict(os.environ, {"USER": user, "LOGNAME": user}, clear=False),
                mock.patch.object(agent_exec.getpass, "getuser", return_value=user),
                mock.patch.object(agent_exec.os, "getgid", return_value=1000),
                mock.patch.object(agent_exec.os, "getgroups", return_value=list(groups_map.keys())),
                mock.patch.object(agent_exec.socket, "gethostname", return_value=hostname),
                mock.patch.object(agent_exec.platform, "node", return_value=hostname),
            ]
            if agent_exec.pwd is not None:
                self._patches.append(mock.patch.object(agent_exec.pwd, "getpwuid", side_effect=_getpwuid))
            if agent_exec.grp is not None:
                self._patches.append(mock.patch.object(agent_exec.grp, "getgrgid", side_effect=_getgrgid))
            for p in self._patches:
                p.__enter__()

        def __exit__(self, *args: object) -> None:
            """Tear down all OS identity patches."""
            for p in reversed(self._patches):
                p.__exit__(*args)

    return _Ctx()


class OsIdentityRedactionTests(unittest.TestCase):
    """Verify agent_exec and devpack_logs redact OS user, group, and host names."""

    def test_agent_exec_redacts_ls_l_user_group_and_host_without_breaking_tools(self) -> None:
        """redact_sensitive_text replaces OS user/group/host while keeping home ~ and generic terms."""
        fake_home = "/" + "ho" + "me/jdoe_eng"
        raw = (
            f"cwd={fake_home}/repo user=jdoe_eng host=workstation-42.corp.internal "
            "short=workstation-42\n"
            "-rw-r--r-- 1 jdoe_eng eng_team 4096 Nov 22 10:00 main.cpp\n"
            "-rwxr-xr-x 1 jdoe_eng docker 512 Nov 22 10:01 run.sh\n"
            "cmd=python3 runner/agent_exec.py --isolation docker\n"
        )
        with _mock_os_context(extra_groups={999: "docker"}):
            redacted = agent_exec.redact_sensitive_text(raw)

        self.assertNotIn("jdoe_eng", redacted)
        self.assertNotIn("eng_team", redacted)
        self.assertNotIn("workstation-42", redacted)
        self.assertIn("~/repo", redacted)
        self.assertIn("[USER]", redacted)
        self.assertIn("[GROUP]", redacted)
        self.assertIn("[HOST]", redacted)
        # Generic group 'docker' is redacted in the ls -l column but preserved in CLI flags.
        self.assertIn("-rwxr-xr-x 1 [USER] [GROUP] 512", redacted)
        self.assertIn("runner/agent_exec.py --isolation docker", redacted)

    def test_agent_exec_redacts_json_escaped_ls_output_in_stream(self) -> None:
        """Raw JSONL tool_result strings with \\n-separated ls -l lines are redacted."""
        jsonl_line = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": (
                                "whoami\njdoe_eng\n"
                                "total 4\n"
                                "-rw-r--r-- 1 jdoe_eng eng_team 128 Nov 22 10:00 a.txt\n"
                                "host=workstation-42"
                            ),
                        }
                    ],
                },
            }
        )
        with _mock_os_context():
            redacted = agent_exec.redact_sensitive_text(jsonl_line)
        self.assertNotIn("jdoe_eng", redacted)
        self.assertNotIn("eng_team", redacted)
        self.assertNotIn("workstation-42", redacted)
        parsed = json.loads(redacted)
        content = parsed["message"]["content"][0]["content"]
        self.assertIn("[USER]", content)
        self.assertIn("[GROUP]", content)
        self.assertIn("[HOST]", content)

    def test_devpack_logs_render_redacts_os_user_group_and_host(self) -> None:
        """devpack_logs.render scrubs OS user, group, and host from transcript.md and index.html."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20261122T120000Z-abcdef"
            steps_dir = run_dir / "steps"
            steps_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": run_dir.name,
                        "kind": "task",
                        "started": "2026-11-22T12:00:00Z",
                        "finished": "2026-11-22T12:00:05Z",
                        "engines": {"implementer": "haiku"},
                        "task": "check-ls",
                        "outcome": "ok",
                    }
                ),
                encoding="utf-8",
            )
            (steps_dir / "01-implementer-haiku.prompt.md").write_text(
                "Run ls -l on workstation-42 for jdoe_eng.\n",
                encoding="utf-8",
            )
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-haiku-5-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_1",
                                "name": "Bash",
                                "input": {"command": "ls -l"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": "-rw-r--r-- 1 jdoe_eng eng_team 64 Nov 22 10:00 b.cpp\n",
                            }
                        ],
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "duration_ms": 50,
                    "total_cost_usd": 0.001,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "result": "Listed directory on workstation-42.corp.internal for jdoe_eng.",
                },
            ]
            (steps_dir / "01-implementer-haiku.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8",
            )
            (steps_dir / "01-implementer-haiku.meta.json").write_text(
                json.dumps(
                    {
                        "role": "implementer",
                        "engine": "haiku",
                        "status": "ok",
                        "prompt_sha256": "0" * 64,
                        "prompt_file": "01-implementer-haiku.prompt.md",
                    }
                ),
                encoding="utf-8",
            )

            with _mock_os_context():
                rc = devpack_logs.main(["render", str(run_dir)])
            self.assertEqual(0, rc)

            for fname in ("transcript.md", "index.html"):
                rendered = (run_dir / fname).read_text(encoding="utf-8")
                self.assertNotIn("jdoe_eng", rendered)
                self.assertNotIn("eng_team", rendered)
                self.assertNotIn("workstation-42", rendered)
                self.assertIn("[USER]", rendered)
                self.assertIn("[GROUP]", rendered)
                self.assertIn("[HOST]", rendered)


    def test_stream_protocol_fields_survive_identity_collision_with_user_and_host(self) -> None:
        """Identity named 'user' and host named 'result' must not corrupt stream-json protocol fields."""
        result_event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "duration_ms": 50,
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "result": "done",
        }
        tool_result_event = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "-rw-r--r-- 1 user user 10 f",
                    }
                ],
            },
        }
        raw_lines = [json.dumps(tool_result_event), json.dumps(result_event)]
        with _mock_os_context(user="user", primary_group="user", hostname="result"):
            redacted_lines = [agent_exec.redact_stream_line(line) for line in raw_lines]
            with tempfile.TemporaryDirectory() as tmp:
                jsonl_path = Path(tmp) / "stream.jsonl"
                jsonl_path.write_text("\n".join(redacted_lines) + "\n", encoding="utf-8")
                parsed = agent_exec.parse_stream(jsonl_path)

        self.assertNotIn("no result event", parsed.error)
        self.assertEqual("ok", parsed.status)
        self.assertEqual("done", parsed.final_text)

        user_event = json.loads(redacted_lines[0])
        self.assertEqual("user", user_event["type"])
        self.assertEqual("user", user_event["message"]["role"])
        self.assertEqual("tool_result", user_event["message"]["content"][0]["type"])
        self.assertEqual(
            "-rw-r--r-- 1 [USER] [GROUP] 10 f",
            user_event["message"]["content"][0]["content"],
        )
        result_obj = json.loads(redacted_lines[1])
        self.assertEqual("result", result_obj["type"])
        self.assertEqual("success", result_obj["subtype"])

    def test_stream_line_preserves_protocol_positions_and_redacts_rest(self) -> None:
        """Protocol values at stream-json positions are preserved; all other keys/values are redacted."""
        line = json.dumps(
            {
                "type": "assistant",
                "uuid": "jdoe_eng",
                "message": {
                    "role": "jdoe_eng",
                    "model": "jdoe_eng",
                    "id": "jdoe_eng",
                    "content": [
                        {"type": "text", "text": "owner is jdoe_eng"},
                        {
                            "type": "tool_use",
                            "id": "jdoe_eng",
                            "name": "jdoe_eng",
                            "input": {"name": "jdoe_eng"},
                        },
                    ],
                },
                "jdoe_eng": "jdoe_eng",
            }
        )
        with _mock_os_context():
            redacted = agent_exec.redact_stream_line(line)
        obj = json.loads(redacted)
        self.assertEqual("assistant", obj["type"])
        self.assertEqual("jdoe_eng", obj["uuid"])
        self.assertEqual("jdoe_eng", obj["message"]["role"])
        self.assertEqual("jdoe_eng", obj["message"]["model"])
        self.assertEqual("jdoe_eng", obj["message"]["id"])
        self.assertNotIn("jdoe_eng", obj)
        self.assertEqual("[USER]", obj["[USER]"])
        self.assertEqual("owner is [USER]", obj["message"]["content"][0]["text"])
        tool_block = obj["message"]["content"][1]
        self.assertEqual("jdoe_eng", tool_block["id"])
        self.assertEqual("jdoe_eng", tool_block["name"])
        self.assertEqual({"name": "[USER]"}, tool_block["input"])

    def test_protocol_exemptions_apply_only_at_protocol_positions(self) -> None:
        """Same key names outside the protocol positions are redacted, not exempted."""
        line = json.dumps(
            {
                "type": "assistant",
                "role": "jdoe_eng",
                "name": "jdoe_eng",
                "id": "jdoe_eng",
                "message": {
                    "content": [
                        {"type": "text", "name": "jdoe_eng", "text": "hi"},
                        {"type": "text", "id": "jdoe_eng", "text": "hi"},
                        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "jdoe_eng"}},
                    ]
                },
            }
        )
        with _mock_os_context():
            obj = json.loads(agent_exec.redact_stream_line(line))
        self.assertEqual("[USER]", obj["role"])
        self.assertEqual("[USER]", obj["name"])
        self.assertEqual("[USER]", obj["id"])
        blocks = obj["message"]["content"]
        self.assertEqual("[USER]", blocks[0]["name"])
        # Block-level ``id`` is a protocol field on every block type, so it stays intact.
        self.assertEqual("jdoe_eng", blocks[1]["id"])
        self.assertEqual("Bash", blocks[2]["name"])
        self.assertEqual("tu_1", blocks[2]["id"])
        self.assertEqual({"command": "[USER]"}, blocks[2]["input"])

    def test_tool_use_input_name_is_redacted_while_tool_name_stays(self) -> None:
        """A tool_use block keeps its tool name but redacts identity values inside its input."""
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_2",
                            "name": "Bash",
                            "input": {"name": "jdoe_eng"},
                        }
                    ],
                },
            }
        )
        with _mock_os_context():
            obj = json.loads(agent_exec.redact_stream_line(line))
        block = obj["message"]["content"][0]
        self.assertEqual("Bash", block["name"])
        self.assertEqual({"name": "[USER]"}, block["input"])

    def test_tool_result_text_list_with_identity_is_redacted(self) -> None:
        """A tool_result whose nested text block names the user has that text redacted."""
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_3",
                            "content": [{"type": "text", "text": "jdoe_eng"}],
                        }
                    ],
                },
            }
        )
        with _mock_os_context():
            obj = json.loads(agent_exec.redact_stream_line(line))
        self.assertEqual("user", obj["message"]["role"])
        result_block = obj["message"]["content"][0]
        self.assertEqual("tool_result", result_block["type"])
        self.assertEqual("tu_3", result_block["tool_use_id"])
        self.assertEqual([{"type": "text", "text": "[USER]"}], result_block["content"])

    def test_redact_stream_line_non_json_gets_full_redaction(self) -> None:
        """Non-JSON stdout lines passed to redact_stream_line undergo full text redaction."""
        fake_home = "/" + "ho" + "me/jdoe_eng"
        fake_secret = "FAKE_STREAM_SECRET_998877"
        raw = f"plain error user=jdoe_eng host=workstation-42 path={fake_home}/app secret={fake_secret}\n"
        with _mock_os_context():
            redacted = agent_exec.redact_stream_line(raw, extra_secrets=[fake_secret])
        self.assertNotIn("jdoe_eng", redacted)
        self.assertNotIn("workstation-42", redacted)
        self.assertNotIn(fake_secret, redacted)
        self.assertIn("~/app", redacted)
        self.assertIn("[USER]", redacted)
        self.assertIn("[HOST]", redacted)
        self.assertIn("<REDACTED_CREDENTIALS>", redacted)
        self.assertTrue(redacted.endswith("\n"))

    def test_redact_value_and_structure_redact_keys_and_have_no_exemptions(self) -> None:
        """redact_value/redact_structure redact dict keys (secrets, paths, OS IDs) with no exemptions."""
        fake_home = "/" + "ho" + "me/jdoe_eng"
        fake_aws_key = "AKIAIOSFODNN7EXAMPLE"
        fake_secret = "FAKE_KEY_SECRET_VAL_7788"
        payload = {
            f"{fake_home}/{fake_aws_key}/{fake_secret}": "jdoe_eng on workstation-42",
            "model": "workstation-42",
            "type": "workstation-42",
            "role": "jdoe_eng",
            "nested": {"jdoe_eng": 1},
        }
        with _mock_os_context():
            redacted_val = agent_exec.redact_value(payload, extra_secrets=[fake_secret])
            redacted_struct = agent_exec.redact_structure(payload, extra_secrets=[fake_secret])

        for out in (redacted_val, redacted_struct):
            serialized = json.dumps(out)
            self.assertNotIn(fake_home, serialized)
            self.assertNotIn(fake_aws_key, serialized)
            self.assertNotIn(fake_secret, serialized)
            self.assertNotIn("jdoe_eng", serialized)
            self.assertNotIn("workstation-42", serialized)
            self.assertEqual("[HOST]", out["model"])
            self.assertEqual("[HOST]", out["type"])
            self.assertEqual("[USER]", out["role"])
            self.assertEqual({"[USER]": 1}, out["nested"])
            expected_key = "~/<REDACTED:aws-access-key-id>/<REDACTED_CREDENTIALS>"
            self.assertIn(expected_key, out)
            self.assertEqual("[USER] on [HOST]", out[expected_key])

    def test_permission_denial_meta_redacts_secret_and_home_path_in_keys(self) -> None:
        """Permission-denial tool_input keys with a secret token and home path are redacted in .meta.json."""
        fake_home = "/" + "ho" + "me/jdoe_eng"
        fake_cred_secret = "FAKE_OAUTH_DENIAL_TOKEN_5566"
        fake_aws_key = "AKIAIOSFODNN7EXAMPLE"
        denial_key = f"{fake_home}/secrets/{fake_cred_secret}/{fake_aws_key}"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            workdir.mkdir()
            cred_file = base / "creds.json"
            cred_file.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": fake_cred_secret}}),
                encoding="utf-8",
            )
            fake_claude = base / "fake_claude.py"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                f"denials = [{{'tool_name': 'Write', 'tool_input': {{{denial_key!r}: 'workstation-42'}}}}]\n"
                "print(json.dumps({\n"
                "    'type': 'result',\n"
                "    'subtype': 'success',\n"
                "    'is_error': False,\n"
                "    'num_turns': 1,\n"
                "    'duration_ms': 10,\n"
                "    'total_cost_usd': 0.001,\n"
                "    'usage': {'input_tokens': 5, 'output_tokens': 2},\n"
                "    'permission_denials': denials,\n"
                "    'result': 'done',\n"
                "}))\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            run_dir = agent_exec.new_run_dir(base / "runs", kind="task", task="t-denial", engines=["haiku"])
            with _mock_os_context():
                res = agent_exec.run_agent(
                    agent_exec.AgentRequest(
                        engine=agent_exec.EngineConfig(name="haiku", model="haiku", engine_class="cloud"),
                        prompt="test denial",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="01-implementer-haiku",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        credentials_path=cred_file,
                        agent_bin=str(fake_claude),
                    )
                )

            self.assertEqual("ok", res.status, msg=res.error)
            meta_path = run_dir / "steps" / "01-implementer-haiku.meta.json"
            jsonl_path = run_dir / "steps" / "01-implementer-haiku.jsonl"
            for artifact in (meta_path, jsonl_path):
                raw_text = artifact.read_text(encoding="utf-8")
                self.assertNotIn(fake_cred_secret, raw_text)
                self.assertNotIn(fake_aws_key, raw_text)
                self.assertNotIn(fake_home, raw_text)
                self.assertNotIn("jdoe_eng", raw_text)
                self.assertNotIn("workstation-42", raw_text)

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            denial_input = meta["permission_denials"][0]["tool_input"]
            self.assertEqual(
                {"~/secrets/<REDACTED_CREDENTIALS>/<REDACTED:aws-access-key-id>": "[HOST]"},
                denial_input,
            )

    def test_run_agent_stream_result_and_role_preserved_on_identity_collision(self) -> None:
        """run_agent parses stream result event when host='result' and preserves role='user'."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            workdir.mkdir()
            fake_claude = base / "fake_claude.py"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tu_1', 'content': '-rw-r--r-- 1 user user 10 f'}]}}))\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'duration_ms': 25, 'total_cost_usd': 0.001, 'usage': {'input_tokens': 4, 'output_tokens': 2}, 'result': 'result host done'}))\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            run_dir = agent_exec.new_run_dir(base / "runs", kind="task", task="t-collide", engines=["haiku"])
            with _mock_os_context(user="user", primary_group="user", hostname="result"):
                res = agent_exec.run_agent(
                    agent_exec.AgentRequest(
                        engine=agent_exec.EngineConfig(name="haiku", model="haiku", engine_class="cloud"),
                        prompt="test collision",
                        workdir=workdir,
                        run_dir=run_dir,
                        step_name="01-implementer-haiku",
                        isolation="local",
                        allow_unsandboxed_local=True,
                        agent_bin=str(fake_claude),
                    )
                )

            self.assertEqual("ok", res.status, msg=res.error)
            self.assertEqual("[HOST] host done", res.final_text)
            lines = [
                json.loads(line)
                for line in (run_dir / "steps" / "01-implementer-haiku.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual("user", lines[0]["message"]["role"])
            self.assertEqual("result", lines[1]["type"])

    def test_devpack_logs_render_keeps_result_event_when_user_and_host_collide(self) -> None:
        """devpack_logs.render keeps protocol fields intact when identities collide with JSON words."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20261122T120000Z-fedcba"
            steps_dir = run_dir / "steps"
            steps_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": run_dir.name,
                        "kind": "task",
                        "started": "2026-11-22T12:00:00Z",
                        "finished": "2026-11-22T12:00:05Z",
                        "engines": {"implementer": "haiku"},
                        "task": "check-ls",
                        "outcome": "ok",
                    }
                ),
                encoding="utf-8",
            )
            events = [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": "-rw-r--r-- 1 user user 10 f",
                            }
                        ],
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "duration_ms": 50,
                    "total_cost_usd": 0.001,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "result": "all listed",
                },
            ]
            (steps_dir / "01-implementer-haiku.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8",
            )
            (steps_dir / "01-implementer-haiku.meta.json").write_text(
                json.dumps(
                    {
                        "role": "implementer",
                        "engine": "haiku",
                        "status": "ok",
                        "prompt_sha256": "0" * 64,
                        "prompt_file": "01-implementer-haiku.prompt.md",
                    }
                ),
                encoding="utf-8",
            )

            with _mock_os_context(user="user", primary_group="user", hostname="result"):
                rc = devpack_logs.main(["render", str(run_dir)])
            self.assertEqual(0, rc)
            transcript = (run_dir / "transcript.md").read_text(encoding="utf-8")

        self.assertIn("-rw-r--r-- 1 [USER] [GROUP] 10 f", transcript)
        self.assertIn("all listed", transcript)
        self.assertNotIn("no result event", transcript)


    def test_devpack_logs_redacts_tool_input_model_hostname_and_has_no_stream_exemptions(self) -> None:
        """devpack_logs and redact_sensitive_text apply no stream exemptions to tool input JSON."""
        tool_input_json = json.dumps({"model": "workstation-42"})
        with _mock_os_context():
            redactor = devpack_logs.Redactor()
            redacted_log = redactor.redact(tool_input_json)
            redacted_text = agent_exec.redact_sensitive_text(tool_input_json)
        self.assertEqual({"model": "[HOST]"}, json.loads(redacted_log))
        self.assertEqual({"model": "[HOST]"}, json.loads(redacted_text))

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20261122T120000Z-112233"
            steps_dir = run_dir / "steps"
            steps_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": run_dir.name,
                        "kind": "task",
                        "started": "2026-11-22T12:00:00Z",
                        "finished": "2026-11-22T12:00:05Z",
                        "engines": {"implementer": "haiku"},
                        "task": "check-tool-input",
                        "outcome": "ok",
                    }
                ),
                encoding="utf-8",
            )
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-haiku-5-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_1",
                                "name": "Bash",
                                "input": {"model": "workstation-42"},
                            }
                        ],
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "duration_ms": 10,
                    "total_cost_usd": 0.001,
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                    "result": "ok",
                },
            ]
            (steps_dir / "01-implementer-haiku.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8",
            )
            (steps_dir / "01-implementer-haiku.meta.json").write_text(
                json.dumps(
                    {
                        "role": "implementer",
                        "engine": "haiku",
                        "status": "ok",
                        "prompt_sha256": "0" * 64,
                        "prompt_file": "01-implementer-haiku.prompt.md",
                    }
                ),
                encoding="utf-8",
            )

            with _mock_os_context():
                rc = devpack_logs.main(["render", str(run_dir)])
            self.assertEqual(0, rc)
            for fname in ("transcript.md", "index.html"):
                rendered = (run_dir / fname).read_text(encoding="utf-8")
                self.assertNotIn("workstation-42", rendered)
                self.assertIn("[HOST]", rendered)


if __name__ == "__main__":
    unittest.main()
