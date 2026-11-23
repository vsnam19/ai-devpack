## Oracle Design & Implementation Guide

### Design
- Implement multiplexed physical signal decoding (`REQ-CODEC-008`) in the `can_codec` module.
- Validate both `muxSelectorSignal` and `valueSignal` via `isValidSignal()`, reject signed or >16-bit selector signals (`muxSelectorSignal.isSigned || muxSelectorSignal.bitLength > 16U`), and reject null `frameBytes` or `physicalOut`.
- Unpack the unsigned raw selector value via `unpackRaw(muxSelectorSignal, frameBytes, kMaxFrameBytes, &actualMux)`.
- Compare `actualMux` against `expectedMuxValue`; on mismatch, return `false` without modifying `*physicalOut`.
- On match, delegate to `decodePhysical(valueSignal, frameBytes, kMaxFrameBytes, physicalOut)` and return its boolean result.

### Interfaces
Declare inside `namespace can_fixture` in `include/can_codec.hpp` with full Doxygen `@brief`, `@param[in]`, `@param[out]`, and `@return` tags:
- `bool decodeMultiplexedPhysical(const SignalDef& muxSelectorSignal, std::uint64_t expectedMuxValue, const SignalDef& valueSignal, const std::uint8_t* frameBytes, double* physicalOut) noexcept;`

### Files to Touch
- `include/can_codec.hpp`: declare `decodeMultiplexedPhysical` with Doxygen documentation.
- `src/can_codec.cpp`: implement `decodeMultiplexedPhysical` using `isValidSignal`, `unpackRaw`, and `decodePhysical`.
- tests/test_can_codec.cpp: add unit tests (`@req REQ-CODEC-008`) covering selector match (signed and big-endian 16-bit boundary), selector mismatch leaving `*physicalOut` untouched, and invalid selector guards (`isSigned == true`, `bitLength > 16`, null pointers).

### Edge Cases
- `muxSelectorSignal.bitLength == 16U` (both Little and Big endian) must be accepted; `bitLength == 17U` must be rejected.
- `*physicalOut` must remain completely unmodified on any validation error or selector mismatch when `physicalOut != nullptr`.
- Follow `preflight.toml` Doxygen and MISRA C++ rules (avoid C-style casts, use `nullptr`, `U`/`ULL` suffixes).
