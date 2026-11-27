"""Local external job fixture."""
import json
import os
from pathlib import Path
import sys
import time

mode, brief, output, role, model, *scope = sys.argv[1:]
if mode == "timeout":
    time.sleep(10)
assert "HIDDEN" not in os.environ
if mode in ("workflow", "workflow-review"):
    assert Path.cwd().name == "worktree"
    assert (role, scope) == (("reviewer", []) if mode == "workflow-review"
                             else ("implement", ["--allow", "src/**"]))
else:
    assert role == "implement"
    assert scope == ["--allow", "literal;touch marker"]
output = Path(output)
reply = output / "reply.md"
reply.write_text(Path(brief).read_text(), encoding="utf-8")
if mode in ("live", "batch"):
    first = json.dumps({"type": "progress", "text": os.environ["FIXTURE_TOKEN"] + "\x1b"})
    last = json.dumps({"type": "done", "text": "finished ☃"}, ensure_ascii=False)
    if mode == "live":
        Path("ready").touch()
        while not Path("start-events").exists():
            time.sleep(0.01)
    with (output / "events").open("w", encoding="utf-8") as events:
        events.write(first + "\ninvalid json\n[]\n" + last[:10])
        events.flush()
        if mode == "live":
            while not Path("finish-events").exists():
                time.sleep(0.01)
        events.write(last[10:])
elif mode not in ("quiet", "workflow", "workflow-review"):
    (output / "events").write_text(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": os.environ["FIXTURE_TOKEN"]},
    }) + "\n", encoding="utf-8")
agent, model_id = model.split(":", 1)
print(json.dumps({
    "status": {"failed": "failed", "quota": "exhausted"}.get(mode, "ok"),
    "exit_code": int(mode == "failed"),
    "reply_path": str(reply),
    "model": model_id,
    "agent": agent,
    **({"verdict": "APPROVE"} if mode == "workflow-review" else {}),
    "role": role,
    "scope": scope,
}))
