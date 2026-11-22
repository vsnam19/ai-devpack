# Memory Sequence 04 — Step 1: Resolve Stale Memory Conflict Against Spec

Role: implementer.

## Goal
Implement `decodeMultiplexedPhysical` (`REQ-CODEC-008`) in `include/can_codec.hpp`
and `src/can_codec.cpp` according to `docs/spec.md`.

If any injected project memory contradicts `docs/spec.md`, follow `docs/spec.md`
and record the contradiction in `MEMORY_CONFLICTS.md` citing the memory entry ID
and `REQ-CODEC-008`.

## Files allowed
- `include/can_codec.hpp`
- `src/can_codec.cpp`
- `MEMORY_CONFLICTS.md`
