# Automotive CAN & Diagnostic Supervision Specification

This document defines the normative functional requirements for the `can_fixture` library modules:
- **CAN Signal Codec** (`include/can_codec.hpp`, `src/can_codec.cpp`)
- **Frame Ring Buffer** (`include/ring_buffer.hpp`, `src/ring_buffer.cpp`)
- **Link State Machine** (`include/link_state_machine.hpp`, `src/link_state_machine.cpp`)
- **Diagnostic Trouble Code (DTC) Debouncer** (`include/dtc_debouncer.hpp`, `src/dtc_debouncer.cpp`)

All functions and methods operate without dynamic heap allocation, exceptions, or RTTI, and use fixed-width integer types.

---

## 1. CAN Signal Codec (`REQ-CODEC-*`)

### REQ-CODEC-001: Signal Definition Validation
`bool isValidSignal(const SignalDef& signal) noexcept` shall return `true` if and only if:
- `signal.bitLength` is in `[1, 64]`;
- `static_cast<std::size_t>(signal.startBit) + static_cast<std::size_t>(signal.bitLength) <= 64U` (`kMaxFrameBits`);
- `signal.endianness` is `Endianness::Little` or `Endianness::Big`;
- `signal.scale`, `signal.offset`, `signal.minPhysical`, and `signal.maxPhysical` are finite (`std::isfinite`);
- `signal.scale > 0.0`;
- `signal.minPhysical <= signal.maxPhysical`.

### REQ-CODEC-002: Little-Endian Raw Bit Packing and Unpacking
For signals with `Endianness::Little`, signal bit `k` (`0` = LSB .. `bitLength - 1` = MSB) maps to frame bit position `pos = startBit + k`, where `byteIndex = pos / 8` and `bitInByte = pos % 8` (`0` = byte LSB .. `7` = byte MSB).
`packRaw` shall insert the lowest `bitLength` bits of `rawValue` without disturbing unrelated bits in the 8-byte frame buffer. `unpackRaw` shall extract the `bitLength` bits as an unsigned 64-bit value.

### REQ-CODEC-003: Big-Endian Raw Bit Packing and Unpacking
For signals with `Endianness::Big`, signal bit offset `m` from the most significant bit (`0` = MSB .. `bitLength - 1` = LSB, so `m = bitLength - 1 - signalBitIndex`) maps to network bit position `pos = startBit + m`, where `byteIndex = pos / 8` and `bitInByte = 7 - (pos % 8)`.

### REQ-CODEC-004: Two's-Complement Sign Extension and Signed Clamping
- `std::int64_t signExtend(std::uint64_t rawValue, std::uint8_t bitLength) noexcept` shall treat bit `bitLength - 1` (`0`-indexed) as the two's-complement sign bit for any `bitLength` in `[1, 63]` and fill upper bits `[bitLength, 63]` with `1` when that sign bit is set.
- When `signal.isSigned == true` (for both `Endianness::Little` and `Endianness::Big`):
  - `decodePhysical` shall apply `signExtend(rawValue, signal.bitLength)` before scaling by `signal.scale` and adding `signal.offset`.
  - `encodePhysical` shall clamp the rounded raw value `std::round((clampedPhysical - signal.offset) / signal.scale)` to the signed `bitLength`-bit two's-complement range `[-(2^(bitLength - 1)), 2^(bitLength - 1) - 1]` before masking to `bitLength` bits and packing.

### REQ-CODEC-005: Physical Scaling, Offset, and Engineering Clamping
- `encodePhysical` shall first clamp `physicalValue` to `[signal.minPhysical, signal.maxPhysical]`, compute `std::round((clampedPhysical - signal.offset) / signal.scale)`, clamp to the raw integer range (`[0, 2^bitLength - 1]` for unsigned, or `[-(2^(bitLength - 1)), 2^(bitLength - 1) - 1]` for signed), and pack the raw bits.
- `decodePhysical` shall unpack the raw bits, convert to signed (if `signal.isSigned`) or unsigned numeric form, compute `(rawNumeric * signal.scale) + signal.offset`, and clamp the resulting physical value to `[signal.minPhysical, signal.maxPhysical]`.

### REQ-CODEC-006: Buffer and Boundary Rejection
`packRaw` and `unpackRaw` shall return `false` when `!isValidSignal(signal)`, `frameBytes == nullptr`, `frameLength < kMaxFrameBytes` (`8`), or (for `unpackRaw`) `rawOut == nullptr`.

### REQ-CODEC-007: Raw Range Verification (`isRawInRange`)
`bool isRawInRange(const SignalDef& signal, std::int64_t candidateValue) noexcept` shall verify whether `candidateValue` fits within the raw integer range of `signal`:
- Returns `false` if `!isValidSignal(signal)`.
- For unsigned signals (`!signal.isSigned`), returns `false` if `candidateValue < 0`; returns `true` if `signal.bitLength >= 63U` or `static_cast<std::uint64_t>(candidateValue) <= ((1ULL << signal.bitLength) - 1ULL)`.
- For signed signals (`signal.isSigned`), returns `true` if `signal.bitLength >= 64U` or `candidateValue` is in `[-(1LL << (signal.bitLength - 1U)), (1LL << (signal.bitLength - 1U)) - 1LL]`.

### REQ-CODEC-008: Multiplexed Signal Decoding (`decodeMultiplexedPhysical`)
Declare in `include/can_codec.hpp` (inside namespace `can_fixture`) and implement in `src/can_codec.cpp`:
```cpp
bool decodeMultiplexedPhysical(
    const SignalDef& muxSelectorSignal,
    std::uint64_t expectedMuxValue,
    const SignalDef& valueSignal,
    const std::uint8_t* frameBytes,
    double* physicalOut) noexcept;
```
Behavior:
- `frameBytes` points to an 8-byte CAN payload buffer of length `kMaxFrameBytes` (`8`).
- Returns `false` (leaving `*physicalOut` unmodified when `physicalOut != nullptr`) if:
  - `!isValidSignal(muxSelectorSignal)` or `!isValidSignal(valueSignal)`;
  - `muxSelectorSignal.isSigned == true` or `muxSelectorSignal.bitLength > 16U`;
  - `frameBytes == nullptr` or `physicalOut == nullptr`.
- Unpacks the unsigned raw selector value from `frameBytes` (length `kMaxFrameBytes`) using `muxSelectorSignal`.
- If the unpacked selector value does not equal `expectedMuxValue`, leaves `*physicalOut` unmodified and returns `false`.
- If the unpacked selector value equals `expectedMuxValue`, decodes `valueSignal` from `frameBytes` (length `kMaxFrameBytes`) into `*physicalOut` via `decodePhysical` and returns its result.

### REQ-CODEC-009: Signal Bit-Position Overlap Detection (`doSignalsOverlap`)
Declare in `include/can_codec.hpp` (inside namespace `can_fixture`) and implement in `src/can_codec.cpp`:
```cpp
bool doSignalsOverlap(const SignalDef& firstSignal, const SignalDef& secondSignal) noexcept;
```
Behavior:
- If `!isValidSignal(firstSignal)` or `!isValidSignal(secondSignal)`, returns `false`.
- Determines the set of physical frame bit indices (`byteIndex * 8U + bitInByte`, in `0..63`) occupied by `firstSignal` and by `secondSignal` according to each signal's `startBit`, `bitLength`, and `endianness` (`Endianness::Little` or `Endianness::Big`).
- Returns `true` if the two signals share at least one physical frame bit position, and `false` if their occupied bit positions are completely disjoint.

---

## 2. Frame Ring Buffer (`REQ-RING-*`)

### REQ-RING-001: FIFO Storage and Full Rejection
`FrameRingBuffer` shall clamp requested capacity to `[1, kMaxRingCapacity]` (`1..16`), store up to `capacity()` complete `CanFrame` structures (preserving `canId`, `dlc`, and all 8 bytes of `payload[0..7]`) in strict FIFO order, and return `false` from `push()` when `isFull()`.

### REQ-RING-002: Overwrite Oldest Frame When Full
`bool pushOverwrite(const CanFrame& frame) noexcept` shall store `frame` at the write head; if the buffer was already full (`size() == capacity()`), it shall advance the read tail past the oldest frame, keep `size() == capacity()`, increment `droppedCount()` by `1`, and return `true`. If the buffer was not full, it shall increment `size()` by `1`, leave `droppedCount()` unchanged, and return `false`.

### REQ-RING-003: Non-Destructive Peek and Clear
`peek(CanFrame* frameOut) const noexcept` shall copy the oldest `CanFrame` (including all 8 payload bytes) into `*frameOut` without modifying buffer state, or return `false` if empty or `frameOut == nullptr`. `clear()` shall reset `size()` and `droppedCount()` to `0`.

### REQ-RING-004: Count Buffered Frames by CAN Identifier (`countByCanId`)
`std::size_t countByCanId(std::uint32_t canId) const noexcept` shall return the number of currently buffered frames whose `frame.canId == canId` without modifying buffer state.

### REQ-RING-005: Selective FIFO Frame Extraction by CAN Identifier (`popByCanId`)
Declare on `FrameRingBuffer` in `include/ring_buffer.hpp` and implement in `src/ring_buffer.cpp`:
```cpp
bool popByCanId(std::uint32_t canId, CanFrame* frameOut) noexcept;
```
Behavior:
- If `frameOut == nullptr` or `isEmpty()`, leaves the buffer unchanged and returns `false`.
- Scans the `size()` currently buffered frames in FIFO order starting from the oldest frame (`m_tail`).
- When the first frame with `frame.canId == canId` is found:
  - Copies that frame into `*frameOut`;
  - Removes that frame from the ring buffer and shifts/compacts any newer frames so that all remaining frames stay in contiguous circular storage in their original FIFO order;
  - Decrements the active frame count (`m_count`) by `1` and updates `m_head` (or `m_tail`) consistently so subsequent `push`, `pushOverwrite`, `pop`, and `peek` calls operate across wrap-around without gaps;
  - Leaves `droppedCount()` unchanged and returns `true`.
- If no buffered frame has `frame.canId == canId`, leaves the buffer state unchanged and returns `false`.

---

## 3. Link State Machine (`REQ-LINK-*`)

### REQ-LINK-001: Handshake Initialization and Acknowledgment
`LinkStateMachine` starts in `LinkState::Idle`. `startConnect(nowMs)` transitions from `Idle` to `Connecting` (recording `lastTimestampMs = nowMs` and `retryCount = 0`) when `isConfigValid()` is `true`. `onHandshakeAck(nowMs)` transitions from `Connecting` to `Active` when `nowMs >= lastTimestampMs()`.

### REQ-LINK-002: Handshake Timeout Retry and Fault Escalation
In `LinkState::Connecting`, when `onTick(nowMs)` observes `nowMs - lastTimestampMs() >= handshakeTimeoutMs`:
- If `retryCount() < maxRetries`, increments `retryCount()`, updates `lastTimestampMs = nowMs`, and remains in `LinkState::Connecting`.
- If `retryCount() >= maxRetries`, transitions to `LinkState::Fault` and updates `lastTimestampMs = nowMs`.

### REQ-LINK-003: Heartbeat Supervision and Degraded Escalation
- In `LinkState::Active`, when `onTick(nowMs)` observes `nowMs - lastTimestampMs() >= heartbeatTimeoutMs`, the state machine transitions to `LinkState::Degraded`, sets `retryCount = 1U`, and updates `lastTimestampMs = nowMs`.
- In `LinkState::Degraded`, when `onTick(nowMs)` observes `nowMs - lastTimestampMs() >= heartbeatTimeoutMs`:
  - If `retryCount() < maxRetries`, increments `retryCount()`, updates `lastTimestampMs = nowMs`, and remains in `LinkState::Degraded`.
  - If `retryCount() >= maxRetries`, transitions to `LinkState::Fault` and updates `lastTimestampMs = nowMs`.
- Calling `onHeartbeat(nowMs)` while in `Active` or `Degraded` with `nowMs >= lastTimestampMs()` transitions to `LinkState::Active`, resets `retryCount` to `0U`, updates `lastTimestampMs = nowMs`, and returns `true`.

### REQ-LINK-004: State Machine Reset
`reset()` shall return `LinkStateMachine` from any state to `LinkState::Idle` with `retryCount() == 0U` and `lastTimestampMs() == 0ULL`.

### REQ-LINK-005: Heartbeat State Guard
`onHeartbeat(nowMs)` shall reject calls (returning `false` without modifying state) when the state machine is in `LinkState::Idle`, `LinkState::Connecting`, or `LinkState::Fault`, or when `nowMs < lastTimestampMs()`.

### REQ-LINK-006: Fault Counter and Controlled Bus-Off Fault Recovery (`faultCount`, `recoverFromFault`)
Declare on `LinkStateMachine` in `include/link_state_machine.hpp` and implement in `src/link_state_machine.cpp`:
```cpp
bool recoverFromFault(std::uint64_t nowMs) noexcept;
std::uint32_t faultCount() const noexcept;
```
Behavior:
- `LinkStateMachine` maintains a private member `std::uint32_t m_faultCount` initialized to `0U` on construction.
- Every transition into `LinkState::Fault` inside `onTick(nowMs)` (whether from `LinkState::Connecting` after handshake retries are exhausted or from `LinkState::Degraded` after heartbeat retries are exhausted) increments `m_faultCount` by `1U`.
- `faultCount()` returns `m_faultCount`.
- `recoverFromFault(std::uint64_t nowMs) noexcept`:
  - Succeeds (returning `true`) only when `state() == LinkState::Fault`, `isConfigValid()` is `true`, and `nowMs >= lastTimestampMs()`.
  - On success, transitions `m_state` from `LinkState::Fault` to `LinkState::Connecting`, resets `m_retryCount = 0U`, updates `m_lastTimestampMs = nowMs`, and preserves `m_faultCount` unchanged.
  - Otherwise (not in `LinkState::Fault`, invalid config, or `nowMs < lastTimestampMs()`), leaves all state unchanged and returns `false`.
- `reset()` resets `m_faultCount` to `0U` in addition to resetting `m_state = LinkState::Idle`, `m_lastTimestampMs = 0ULL`, and `m_retryCount = 0U`.

---

## 4. Diagnostic Trouble Code (DTC) Debouncer (`REQ-DTC-*`)

### REQ-DTC-001: Debouncer Configuration Validation and Initial State
`DtcDebouncer` is constructed in `DtcState::Inactive` with `counter() == 0`, `healingCounter() == 0U`, and `isConfirmed() == false`.
`isConfigValid()` returns `true` if and only if:
- `1 <= confirmThreshold <= kMaxDebounceThreshold` (`16383`);
- `-kMaxDebounceThreshold <= clearThreshold <= 0`;
- `1 <= failStep <= kMaxDebounceThreshold`;
- `1 <= passStep <= kMaxDebounceThreshold`;
- `agingThreshold > 0U`.
When `!isConfigValid()`, `reportSample()` returns the current state without modifying counters, and `onOperationCycleEnd()` returns `false`.

### REQ-DTC-002: Asymmetric Pass/Fail Step Counting and Confirmation Latch
`DtcState reportSample(bool isFaultPresent) noexcept`:
- When `isFaultPresent == true`:
  - Increments `counter` by `failStep`, clamped at `confirmThreshold`.
  - Resets `healingCounter` to `0U`.
  - If `counter >= confirmThreshold`, latches confirmation (`isConfirmed() == true`).
  - If latched (`isConfirmed() == true`), sets state to `DtcState::Confirmed` (including when a fault recurs while previously in `DtcState::Healing`).
  - Otherwise, if `counter > 0`, sets state to `DtcState::Pending`; if `counter <= 0`, sets state to `DtcState::Inactive`.
- When `isFaultPresent == false`:
  - Decrements `counter` by `passStep`, clamped down to `clearThreshold`.
  - If not latched (`!isConfirmed()`): sets state to `DtcState::Pending` when `counter > 0`, or `DtcState::Inactive` when `counter <= 0`.
  - If latched (`isConfirmed()`): keeps confirmation latched (`DtcState::Confirmed`, or `DtcState::Healing` if already healing at `clearThreshold`).

### REQ-DTC-003: Operation-Cycle Aging and Healing Transitions
`bool onOperationCycleEnd() noexcept`:
- Returns `false` without changing state if `!isConfigValid()`, `!isConfirmed()`, or `counter() > clearThreshold`.
- When `isConfirmed() == true` and `counter() == clearThreshold`:
  - Increments `healingCounter` by `1U`.
  - If `healingCounter < agingThreshold`, sets state to `DtcState::Healing` (keeping `isConfirmed() == true`) and returns `true`.
  - If `healingCounter >= agingThreshold`, clears the latched fault (`isConfirmed() == false`), resets `healingCounter = 0U`, resets `counter = 0` (so subsequent fault samples count up from `0`, not from a negative `clearThreshold`), sets state to `DtcState::Inactive`, and returns `true`.
- Any failed sample (`reportSample(true)`) while in `DtcState::Healing` must immediately reset `healingCounter` to `0U` and return state to `DtcState::Confirmed`.

### REQ-DTC-004: Diagnostic Clear / Reset
`void reset() noexcept` shall unconditionally reset `state()` to `DtcState::Inactive`, `counter()` to `0`, `healingCounter()` to `0U`, and `isConfirmed()` to `false`.

### REQ-DTC-005: Diagnostic Monitor Freeze Control (`setFrozen`, `isFrozen`)
Declare on `DtcDebouncer` in `include/dtc_debouncer.hpp` and implement in `src/dtc_debouncer.cpp`:
```cpp
void setFrozen(bool frozen) noexcept;
bool isFrozen() const noexcept;
```
Behavior:
- `DtcDebouncer` maintains a private boolean member `m_isFrozen` initialized to `false` on construction.
- `setFrozen(bool frozen) noexcept` sets `m_isFrozen = frozen`.
- `isFrozen() const noexcept` returns `m_isFrozen`.
- While `isFrozen() == true`:
  - `reportSample(bool isFaultPresent)` shall not modify `m_counter`, `m_healingCounter`, `m_state`, or `m_isLatched`, and shall return the current `m_state`.
  - `onOperationCycleEnd()` shall not modify `m_counter`, `m_healingCounter`, `m_state`, or `m_isLatched`, and shall return `false`.
- `reset()` shall also clear `m_isFrozen` back to `false`.

### REQ-DTC-006: Fault Confirmation Margin Query (`remainingToConfirm`)
Declare on `DtcDebouncer` in `include/dtc_debouncer.hpp` and implement in `src/dtc_debouncer.cpp`:
```cpp
std::int16_t remainingToConfirm() const noexcept;
```
Behavior:
- If `!isConfigValid()`, returns `0`.
- If `isConfirmed() == true` (in `DtcState::Confirmed` or `DtcState::Healing`) or `counter() >= confirmThreshold`, returns `0`.
- Otherwise, returns the positive counter distance `static_cast<std::int16_t>(confirmThreshold - counter())` remaining until `counter()` reaches `confirmThreshold`.
