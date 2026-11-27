# AVC triage

Capture test ID/time, PID/executable/domain, object context/path/class, requested
permissions, syscall/result, DAC mode/owner, mount and enforcing state. Correlate the
AVC to that exact operation. The offline tool records these four classifications and requires a reviewed disposition:

| Label | Decision |
| --- | --- |
| covered-by-approved-item | Approved domain/object/class/permissions match: investigate labeling or a missing/incorrect approved rule; do not expand access. |
| missing-in-manifest | Stop permission work. Ask whether access is intended, add purpose and evidence to draft manifest, return to human `selinux-manifest` approval before policy changes. |
| likely-code-bug | Fix unintended access, wrong path, stale FD or incorrect runtime behaviour. Do not add permissions. Re-run affected test. |
| label-problem | Compare actual label/creator to approved label design; fix .fc/BSP setfiles/udev or runtime transition and revalidate. Do not allow the incorrect generic type. |

If evidence cannot distinguish these, mark unknown and ask in QUESTIONS.md; do not
invent a grant-producing category. A policy correction must stay inside approved access.
The parser matches exact domain, path (or explicit type binding), a known class and
permissions. Generic target labels take precedence; denied matches precede approved
matches. Only an absolute audit `path` identifies a file; `name` never substitutes
for it. A missing absolute path is written as `path: unknown` with a question.
The files/denied, data_dirs, devices and unix_sockets categories default to file,
dir, chr_file and sock_file respectively. Other categories require an explicit
item or binding class; otherwise they are skipped with a reason in `skipped_items`. Unmatched
records remain draft proposals, with the complete AVC preserved in `expression`. Track resolved disposition per AVC.

Per-domain permissive diagnostics require an authorized target session with prior-state
capture and guaranteed rollback. Never set global permissive. Restore the domain to
enforcing before Allow/Deny release tests. Verify all permissive entries are absent.

A Deny result passes only if the object exists and DAC permits the tested operation,
the system/domain is enforcing, and the matching AVC proves SELinux blocked it.
EACCES alone, missing paths, EROFS, skipped tests and PASS banners are insufficient.
Audit target Deny helpers for EROFS acceptance and ENOENT skips; those outcomes
cannot prove MAC enforcement.
FD passing needs both fd-use and underlying-object tests in each approved direction;
socket creation success alone does not prove endpoint traffic or peer authentication.

Run `python3 scripts/avc_triage.py --log audit.log --manifest approved.json --profile platform.toml`.
See [tooling](tooling.md) for output paths and review boundaries.
