# AVC triage

Capture test ID/time, PID/executable/domain, object context/path/class, requested
permissions, syscall/result, DAC mode/owner, mount and enforcing state. Correlate the
AVC to that exact operation. Record one of these three labels and a disposition:

| Label | Decision |
| --- | --- |
| missing-in-manifest | Stop permission work. Ask whether access is intended, add purpose and evidence to draft manifest, return to human `selinux-manifest` approval before policy changes. |
| code bug | Fix unintended access, wrong path, stale FD or incorrect runtime behaviour. Do not add permissions. Re-run affected test. |
| wrong label | Compare actual label/creator to approved label design; fix .fc/BSP setfiles/udev or runtime transition and revalidate. Do not allow the incorrect generic type. |

If evidence cannot distinguish these, mark unknown and ask in QUESTIONS.md; do not
invent a fourth grant-producing category. A missing approved rule is handled through
missing-in-manifest review of the approved baseline and mapping discrepancy; a policy
correction must stay inside approved access. Track resolved disposition per AVC.

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
