# Access manifest contract

Use [manifest.schema.json](manifest.schema.json) for structural validation and
[example-manifest.yaml](example-manifest.yaml) as a synthetic draft. JSON flow syntax
is used for the example and extractor output: it is valid YAML 1.2 without PyYAML.

Every identity/resource/process record requires `id`, `why`, `evidence` (workspace-relative
`path:line` strings), and `status` (`draft|approved`). IDs must remain unique across
categories and stable across review revisions. `processes` associates a resource with
executable names; `unknown` requires resolution. All categories are present even if empty.
`identity` contains `runs_as`, `sandboxed` (boolean or `unknown`), `groups` and the common
review fields. A multi-identity app needs separate manifests per identity/process group;
never silently merge distinct identities. `processes` records executable declarations;
review actual run arguments and ownership rather than assuming all executables run.

| Category | Resource-specific fields |
| --- | --- |
| files, data_dirs | path, access (read/write/create/search/unlink as applicable) |
| devices | path, access, ioctls (names/numbers; resolve constants against SDK) |
| unix_sockets | path, access (also record peer and runtime label questions) |
| ipc | api, role client/server, peers |
| fd_passing | object, class, to, from; use exact SELinux object class, not just `fd` |
| network | proto, addr, port (integer or unknown), direction inbound/outbound/both/unknown |
| capabilities | name; distinguish Linux declaration from SELinux permission |
| exec | path, access; include shared-library read/map and helper execution |
| proc_sys, config_tree | path, access |
| env_allowed | name; trace environment-derived paths to resource records |
| limits | name, value; include start/faultAction and process run declarations |
| inherited_grants | deviation_id, grant, source (base-policy path:line) |
| denied | path; optional original resource fields and candidate_denied |

`app`, `version`, `approval {by,date,version}` and `unknowns` complete the root.
Approval date should be ISO 8601; version must identify the exact reviewed content.
Empty approval fields, empty reasons/evidence or any unknown are draft signals. Schema
validation alone never authorizes policy: the human checkpoint must resolve applicable
unknowns, assign process ownership, approve every effective grant/deviation and record
nonempty approval metadata. Approved baseline content must be versioned alongside policy.

Use separate IDs for different process access to the same path; do not union permissions.
Model runtime tmpfs, socket creation/labels, shared library mapping, FD creator/receiver
and underlying object access separately. A path is not a SELinux type. Config-tree path
permissions and sandbox requires remain separate from SELinux IPC permissions.

The extractor recognizes definition blocks lexically, follows resolvable components and
source declarations, and preserves evidence. It cannot evaluate build variables or infer
runtime arguments. `candidate_denied` marks simulation/test branches for human review;
production `#else` observations remain draft grants. Review all conditional evidence.
