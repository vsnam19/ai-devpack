# Task: 02-bugfix-ring-overwrite

Category: bugfix

## Goal

Fix a defect in `src/ring_buffer.cpp` in `FrameRingBuffer::pushOverwrite(const CanFrame& frame)`.

When the ring buffer is full (`m_count >= m_capacity`), `pushOverwrite` writes the new frame at `m_head` and increments `m_droppedCount`, but must also advance `m_tail` to the next oldest slot (`(m_tail + 1U) % m_capacity`) while keeping `m_count == m_capacity` so subsequent `pop()` and `peek()` calls return the remaining frames in strict FIFO order (`REQ-RING-002`).

## Files allowed

- `src/ring_buffer.cpp`

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `src/ring_buffer.cpp`. Do not modify `include/`, `tests/`, `CMakeLists.txt`, `requirements.txt`, or `build_and_test.sh`.
- Never weaken or delete any test assertion.
- Maximum 2 fix attempts per distinct error; if still failing or if blocked by contradictory requirements, stop and report the exact failure.
