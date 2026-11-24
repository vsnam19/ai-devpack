# Anti-patterns and inherited deviations

- Do not turn AVCs or audit2allow output into permission requests without functional
  evidence and human manifest approval. Diagnostics do not define intended behaviour.
- Do not hide failures with permissive/unconfined declarations, global permissive,
  dontaudit, vendor base-policy edits, wildcard grants or weakened compile assertions.
- Do not treat a source allow census, `# M:<ID>`, or a successful .pp build as proof of
  least privilege. The authoritative comparison is effective rules per domain from
  the linked policy against the human-approved baseline.
- Never confuse DAC/sandbox denial, read-only mounts or absent objects with MAC proof.
- Never grant generic device_t or generic writable-tree access merely to fix labeling.
- Never equate a pathname, an IPC API name, a process name and a SELinux type.
- Never silently authorize simulation/test-only probes for production.

## Deviation rule

Read every `base_inherited_grants` entry and platform-specific `banned` item from
the selected profile. Expand the linked base policy to discover additional inherited
access. These entries are review obligations, never authorization for local copies.

Record EACH inherited effective grant/scope under `inherited_grants` with a unique
`deviation_id`, base-policy evidence, affected processes, reason, and review status.
Approve deviations explicitly with the baseline. If inherited access conflicts with
an approved denial or cannot be accepted, stop and raise a platform-owner question;
never edit vendor policy or conceal it in a passing app gate. An allow module cannot
subtract inherited allows. Resolve the platform decision before release.
