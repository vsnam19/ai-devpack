## Oracle Design & Implementation Guide

### Root Cause & Design
- **Module**: `can_codec` (`src/can_codec.cpp`), NOT `ring_buffer.cpp`.
- **Defect**: `packRaw()` in `src/can_codec.cpp` has a byte-aligned little-endian shortcut (`startBit % 8 == 0 && bitLength <= 8`) that overwrites the entire target byte (`frameBytes[startBit / 8U] = static_cast<uint8_t>(maskedValue)`), clobbering unrelated upper bits in the same byte when another signal (e.g. a 4-bit counter in bits `4..7`) was previously packed into that byte.
- **Expected behavior (`REQ-CODEC-002`)**: `packRaw()` must always perform bit-level masked insertion for every bit in `[0, bitLength)`, preserving all unrelated bits in the 8-byte frame buffer. `FrameRingBuffer::pushOverwrite()` (`src/ring_buffer.cpp`) already retains payloads faithfully.

### Interfaces
- `bool packRaw(const SignalDef& signal, std::uint64_t rawValue, std::uint8_t* frameBytes, std::size_t frameLength) noexcept` (`include/can_codec.hpp`, `src/can_codec.cpp`)

### Files to Touch
- `src/can_codec.cpp`: remove the whole-byte overwrite fast-path in `packRaw()` so all signals use the bit-level read-modify-write loop.
- Optional boundary regression test in tests/test_can_codec.cpp (without modifying existing tests).

### Edge Cases
- Co-packed sub-byte signals sharing the same byte (e.g., bits `0..3` and bits `4..7`) packed in any order must preserve each other's bits.
- Byte-aligned `bitLength == 8` signals must also clear/set only their 8 bits cleanly via the bit loop.
- Do not modify `src/ring_buffer.cpp` or existing tests in tests/test_ring_buffer.cpp.
