# Review and gate checklist

Record pass/fail/unknown and evidence for each applicable check. Unknown blocks the
dependent gate. Use `python3 scripts/sel_gate.py` for the implemented supplementary checks;
see [tooling](tooling.md). Live-target and BSP integration checks remain human obligations.

1. Platform: active LSM, enforcing state, policy mcs/mls, kernel policy version,
   per-domain permissive capability, matching SDK include and linked-policy artifact.
2. Identity: dedicated user justification, sandboxed, requires, groups, process run
   settings, start, faultAction and limits approved before any AVC experiment.
3. Inventory: all process/component sources and shared libraries accounted for;
   dynamic resources, APIs/peers, network tuples, runtime sockets/tmpfs, FD classes
   and object origins reviewed. SMACK-label peer authentication audited.
4. Manifest: unique IDs, purpose, evidence, process ownership, explicit denials;
   unknowns resolved for affected steps; named human approved exact version at
   `selinux-manifest`, including base-policy deviations. Schema validation alone fails
   this approval check. Any change in requested access requires renewed approval.
5. Module: profile `module_files`, `build` includes, app attachment and
   `module_bundle_path` verified. Generator output reviewed; no executable renaming. App-name
   transition checked separately from per-executable domains.
6. Static policy: link against exact BSP/base policy with assertions enabled. Inspect
   effective source/target/class/permission tuples for every domain, attribute members,
   conditionals, constraints, transitions and ioctl xperms against approved baseline.
   Record tool versions, build inputs and artifact digest. Missing linked artifact fails.
   `# M:<ID>` supports source review only; reject unexplained or unapproved inherited grants.
7. Labels: file_contexts aggregate nonempty; exact executable/data/library labels;
   external paths integrated through BSP; boot/device recreation and runtime object
   transitions verified. Require explicit build failure on empty contexts.
8. Target: temporary per-domain diagnostics authorized, supported and cleaned up;
   triage each AVC; return new requirements to approval. Never change identity after
   AVC collection without restarting affected review and validation.
9. Release: enforcing globally and for every domain, module loaded, intended domains
   running, no unconfined workaround, Allow tests succeed. Deny tests require DAC-open
   present objects, enforcing mode and correlated AVCs. No EROFS/ENOENT/skipped-test pass.
   Confirm after reboot/update; archive approval, linked baseline and target results.
