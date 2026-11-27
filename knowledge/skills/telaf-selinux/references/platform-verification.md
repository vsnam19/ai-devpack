# Platform verification before tooling

Run read-only commands on the intended target first; record stdout/stderr, revision,
time and tool availability. These are suggested probes, not observed target results.
Every command below is `[verify on target]`; unavailable tools mean unknown, not pass.
External command semantics require confirmation against the target manuals
[src: Linux kernel LSM/SELinux documentation; SELinuxProject policycoreutils manuals — verify].

| Command | Interpret and record |
| --- | --- |
| `[verify on target] cat /sys/kernel/security/lsm` | Active stacked LSMs; selinux vs smack. Missing securityfs is unknown, not proof neither exists. |
| `[verify on target] cat /proc/mounts` | Check selinuxfs, smackfs, securityfs and relevant object mount flags. |
| `[verify on target] sestatus` | Enabled/enforcing state, loaded policy name, policy version and MLS support; compare with BSP. |
| `[verify on target] getenforce` | Must report Enforcing for release. Disabled blocks SELinux deployment. |
| `[verify on target] cat /etc/selinux/config` | SELINUXTYPE mcs/mls and boot intent; configuration is not current enforcing state. |
| `[verify on target] cat /sys/fs/selinux/policyvers` | Kernel supported policy version; resolve compiler compatibility with BSP. |
| `[verify on target] semodule -l` | Required module loaded; module name alone does not prove intended effective rules. |
| `[verify on target] semanage permissive -l` | Enumerate permissive domains; nonempty entries block release, unavailable command leaves unknown support/state. |
| `[verify on target] semanage permissive -h` | Inspect tooling support only; kernel/BSP support still needs authorized controlled validation. |
| `[verify on target] ps -eZ` | Inspect actual supervisor and app domains, matching executable/PID identity. |
| `[verify on target] cat /proc/self/attr/current` | Current process context; audit peer code comparing this interface against SMACK labels. |
| `[verify on target] ls -lZ /legato /etc /data /var/lib /dev` | Observe labels and DAC; inspect exact requested objects separately, do not infer children. |

Supply specific PID/path commands after discovery; do not substitute guessed identities.
Read peer authentication source before target testing; SMACK string comparisons under
SELinux need code review (`framework/daemons/linux/supervisor/proc.c:1498–1502,1538–1543`).
The selected framework/BSP revision may differ from the investigated checkout.

On the build host, request SDK/refpolicy include, tool versions, policy ABI/MLS settings,
external interface dependencies, selected framework revision, image-label recipes and
linked-policy artifact. Check configured `sdk_policy_include` exists and actually links
against the supplied base policy. Absence of documentation must become QUESTIONS.md,
not a guessed compiler command. Investigated Makefiles delegate to an external include;
SDK macro expansion is not established by source module text
(resolve the selected profile interfaces and their definitions).

Only after manifest approval and static gate, an authorized diagnostic session may use
`[verify on target] semanage permissive -a <verified-domain-from-profile>` for the verified domain;
record its prior state and restore with
`[verify on target] semanage permissive -d <verified-domain-from-profile>` in guaranteed cleanup.
These are templates, not commands to run before substituting the approved domain.
Never use setenforce 0. Confirm cleanup with the read-only probes above. If per-domain
support cannot be verified, stop and request an enforcing diagnostic plan.
