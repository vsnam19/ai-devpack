---
id: mem-codec-stale
type: fact
scope: project
tags:
  - REQ-CODEC-008
  - can_codec
  - decodeMultiplexedPhysical
created: 2026-11-01T08:00:00Z
source: human
supersedes: ""
expires: ""
status: active
---

# Stale note on decodeMultiplexedPhysical

For `REQ-CODEC-008`, `decodeMultiplexedPhysical` permits `muxSelectorSignal.bitLength`
up to `32U` and allows signed selector signals (`isSigned == true`).
