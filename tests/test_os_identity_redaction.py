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
            redacted_lines = [agent_exec.redact_sensitive_text(line) for line in raw_lines]
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

    def test_identity_redaction_never_touches_json_keys_or_protocol_values(self) -> None:
        """Keys and protocol values are preserved while free-text string values are redacted."""
        line = json.dumps(
            {
                "type": "assistant",
                "role": "jdoe_eng",
                "model": "jdoe_eng",
                "id": "jdoe_eng",
                "name": "jdoe_eng",
                "message": {"content": [{"type": "text", "text": "owner is jdoe_eng"}]},
                "jdoe_eng": "jdoe_eng",
            }
        )
        with _mock_os_context():
            redacted = agent_exec.redact_sensitive_text(line)
        obj = json.loads(redacted)
        self.assertEqual("assistant", obj["type"])
        self.assertEqual("jdoe_eng", obj["role"])
        self.assertEqual("jdoe_eng", obj["model"])
        self.assertEqual("jdoe_eng", obj["id"])
        self.assertEqual("jdoe_eng", obj["name"])
        self.assertIn("jdoe_eng", obj)
        self.assertEqual("[USER]", obj["jdoe_eng"])
        self.assertEqual("owner is [USER]", obj["message"]["content"][0]["text"])

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


    def test_devpack_logs_redactor_keeps_json_protocol_fields_on_whole_lines(self) -> None:
        """Redactor.redact on a raw stream-json line keeps keys and protocol values intact."""
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "role": "user",
                "result": "listed result files",
            }
        )
        with _mock_os_context(user="jdoe_eng", primary_group="eng_team", hostname="result"):
            redactor = devpack_logs.Redactor()
            redacted = redactor.redact(line)
        obj = json.loads(redacted)
        self.assertEqual("result", obj["type"])
        self.assertEqual("success", obj["subtype"])
        self.assertEqual("user", obj["role"])
        self.assertEqual("listed [HOST] files", obj["result"])


if __name__ == "__main__":
    unittest.main()
