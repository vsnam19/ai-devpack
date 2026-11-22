# Task: 06-small-feature-ring-count-id

Category: small-feature
Tier: smoke

## Goal

Implement a new read-only query method `countByCanId` on `can_fixture::FrameRingBuffer` satisfying requirement `REQ-RING-004` (from `requirements.txt`):

`REQ-RING-004: FrameRingBuffer countByCanId shall return the number of currently buffered frames matching a given canId without modifying buffer state.`

Specifically:
1. In `include/ring_buffer.hpp`, declare on `FrameRingBuffer`:
   ```cpp
   std::size_t countByCanId(std::uint32_t canId) const noexcept;
   ```
   with complete Doxygen documentation (`@brief`, `@param[in] canId`, `@return`).
2. In `src/ring_buffer.cpp`, implement `FrameRingBuffer::countByCanId(std::uint32_t canId) const noexcept` to iterate across the `m_count` active slots starting from `m_tail` (wrapping modulo `m_capacity`) and return how many buffered frames have `frame.canId == canId`.
3. In `test_ring_buffer.cpp` (in `tests/`), add a unit test tagged `@req REQ-RING-004` and call it from `runRingBufferTests()`.

## Files allowed

- `include/ring_buffer.hpp`
- `src/ring_buffer.cpp`
- `test_ring_buffer.cpp` (in `tests/`)

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `include/ring_buffer.hpp`, `src/ring_buffer.cpp`, and `test_ring_buffer.cpp` (in `tests/`).
- Follow all Doxygen (`@param[in]`) and naming rules (no non-meaningful names such as `tmp`, `val`, `buf`).
- Never delete or weaken existing tests or assertions.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
