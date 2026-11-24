---
name: legato-selinux
description: Use when reviewing SELinux policies, access manifests, sandbox permissions or AVCs for Legato apps on a project-configured platform.
---

# Legato SELinux and sandbox workflow

Produce a reviewable per-process access manifest before writing policy. Treat extraction
as evidence gathering, never authorization. Read only references needed for the current step.

## Safety boundary

- Never add permissions absent from the approved manifest and approved inherited-grant baseline.
- Never use audit2allow output as policy. Never edit vendor base policy.
- Never set global permissive, declare `permissive` in policy, or use unconfined domains.
- The sole permissive exception is a human-authorized, temporary **per-domain** diagnostic
  session in step 8, with verified support and an explicit rollback; never release it.
- Stop dependent work and emit `QUESTIONS.md` when inputs are missing. Mark facts `unknown`;
  do not guess SDK macros, identities, labels, API peers, target behaviour or approvals.
- Missing Legato tree/docs/SDK still permits draft inventory, questions and manifest review;
  it blocks steps requiring their evidence. A script success is not a security verdict.

## Workflow

1. **Verify platform before tooling.** Use [platform verification](references/platform-verification.md).
   Record active LSM (SELinux versus SMACK), SELINUXTYPE mcs/mls, loaded policy version,
   per-domain permissive support and matching SDK/refpolicy include. Stop SELinux deployment
   if SELinux is inactive. Inspect peers reading `/proc/<pid>/attr/current`: SMACK-label
   comparisons need application changes under SELinux, not an extra allow rule.
2. **Decide identity and sandbox first.** Review dedicated `username`, groups, `sandboxed`,
   `requires`, `start`, resource limits and `faultAction`; do this BEFORE collecting AVCs.
   Record decisions with reasons and evidence. Do not infer root or sandbox defaults.
3. **Inventory each process.** Resolve .adef/.cdef executables, processes, components,
   sources, bindings, exports, helper execs, shared libraries, runtime tmpfs/sockets and
   FD-passed object classes and creator/receiver domains. Read build-expanded definitions.
   Use the extractor below; manually reconcile dynamic paths and generated/conditional code.
4. **Draft the manifest.** Read [schema guidance](references/manifest-schema.md) and the
   [synthetic example](references/example-manifest.yaml). Include access, purpose, path:line
   evidence, process ownership and explicit denied operations. Keep every extracted item
   `status: draft`, `why: ""`. Review `candidate_denied` test/simulation observations;
   they are neither production grants nor confirmed prohibited operations.
5. **Human checkpoint `selinux-manifest`.** Present identity/sandbox decisions, every item,
   unresolved questions and inherited-grant deviations. Obtain a named human's approval
   of the exact manifest version; record `approval.by/date/version` and approved item
   statuses. Do not fabricate approval. New access returns to this checkpoint.
6. **Generate policy only after approval.** Follow [mapping](references/platform-mapping.md).
   Read the project [platform profile](references/platform-profile.md). Use its optional
   `skeleton_generator` only for a reviewed skeleton; source inventory remains mandatory.
   Never rename executables to accommodate it. Supply the app-name transition checked by
   the supervisor, and separately model each executable domain. Produce the profile `module_files`, use its `build` include variables,
   and attach/package the module at `module_bundle_path`. Prefer pre-linking
   into the image. Add `# M:<ID>` to source grants for review traceability.
7. **Static gate.** Apply [review checklist](references/review-checklist.md) and
   [anti-patterns](references/anti-patterns.md). Compile/link with the matching SDK and
   BSP policy. Compare effective rules per domain in the LINKED policy against the approved
   baseline, including inherited attributes/macros and approved deviations. Source `# M:<ID>`
   checks are supplementary. Fail on empty file_contexts, unresolved labels, extra effective
   grants, missing app transition or missing SDK/linked policy; report blocked, never pass.
8. **Target diagnostics.** Obtain authorization for temporary per-domain permissive AVC
   collection only after step 7 and identity decisions. If unsupported, stop and ask for a
   target-supported enforcing diagnostic plan. Record prior state, scope and cleanup;
   restore enforcing for the domain even on failure. Classify every AVC with
   [AVC triage](references/avc-triage.md). No automatic permission growth.
9. **Enforcing release.** Require global enforcing, no permissive domains, module loaded,
   correct process domains and labels, nonempty applied contexts and working Allow tests.
   Deny tests pass only with a correlated AVC, a DAC-open object and enforcing mode;
   missing files, EROFS and generic EACCES are not proof. Record target/build/manifest
   versions and effective-rule baseline; unresolved deviations block release approval.

## Workspace configuration and extractor

Document this optional section in `.devpack/project.toml` (do not invent local paths):

```toml
[selinux]
platform_profile = "profiles/platform.toml"
platform_root = ""
app_globs = ["**/apps/*.adef"]
manifest_name = "access-manifest.yaml"
module_dir = "{app_dir}/../selinux"
sdk_policy_include = ""
linked_policy = ""
exclude = []
```

All configuration paths/globs are workspace-root-relative. Empty paths mean unknown.
`module_dir` is a template: substitute workspace-relative `app_dir`, normalize, reject
escape outside the workspace. It is guidance for policy generation, not extractor output.
`platform_root` locates optional platform evidence; it does not change the root for app globs.
The extractor reads `[selinux]` independently; the existing workspace scope loader's
component selection remains separate. Reconcile exclusions and active components manually.

Worked example (run from the workspace root, choose a new output directory):

```sh
python3 <skill-dir>/scripts/extract_manifest.py apps/sample.adef --workspace . --output review/sample
python3 <skill-dir>/scripts/extract_manifest.py --app-name sample --workspace . --output review/discovered
```

Replace `<skill-dir>` with this installed skill directory. `--config` accepts project TOML
(Python 3.11+) or a JSON object with a `selinux` member on older Python 3. Output uses JSON
flow syntax, valid YAML 1.2, in the configured manifest filename plus `QUESTIONS.md`.
Existing manifests/questions are never overwritten. Missing files become unknowns;
ambiguous discovery/config errors exit 2. The scanner is lexical and conservative:
macros, includes, multiline calls and build conditions need human reconciliation.
It never invokes build tools, changes target state or emits policy.

The profile is the sole source of platform naming, macros, loader transition checks,
bundle layout, inherited grants and build conventions. See [profile schema](references/platform-profile.md)
and the [fictional example](references/example-platform-profile.toml). Missing/invalid fields
are `unknown` questions; no example value is a fallback. Each base inherited grant seeds
a draft deviation requiring human approval and effective-rule verification.
