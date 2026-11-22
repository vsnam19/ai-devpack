/**
 * @file test_ring_buffer.cpp
 * @brief Unit tests for FrameRingBuffer FIFO storage, overwrite, peek, and clear operations.
 */

#include "ring_buffer.hpp"
#include "test_framework.hpp"

namespace can_fixture_test {

namespace {

using can_fixture::CanFrame;
using can_fixture::FrameRingBuffer;

/**
 * @brief Construct a sample CanFrame with a given CAN identifier and first byte tag.
 * @param[in] canId Frame CAN identifier.
 * @param[in] firstByte First payload byte value.
 * @return Initialized CanFrame struct.
 */
CanFrame makeSampleFrame(std::uint32_t canId, std::uint8_t firstByte) noexcept {
    CanFrame frame{canId, 8U, {firstByte, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    return frame;
}

/**
 * @brief Verify FIFO ordering and capacity rejection when full.
 * @req REQ-RING-001
 */
void testRingBufferFifoAndFullRejection() {
    FrameRingBuffer ring(3U);
    CHECK(ring.isEmpty());
    CHECK(!ring.isFull());
    CHECK_EQ(ring.capacity(), static_cast<std::size_t>(3U));

    CHECK(ring.push(makeSampleFrame(0x101U, 1U)));
    CHECK(ring.push(makeSampleFrame(0x102U, 2U)));
    CHECK(ring.push(makeSampleFrame(0x103U, 3U)));
    CHECK(ring.isFull());
    CHECK_EQ(ring.size(), static_cast<std::size_t>(3U));

    CHECK(!ring.push(makeSampleFrame(0x104U, 4U)));
    CHECK_EQ(ring.size(), static_cast<std::size_t>(3U));

    CanFrame dequeued{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(ring.pop(&dequeued));
    CHECK_EQ(dequeued.canId, 0x101U);
    CHECK_EQ(dequeued.payload[0], static_cast<std::uint8_t>(1U));

    CHECK(ring.push(makeSampleFrame(0x104U, 4U)));
    CHECK(ring.pop(&dequeued));
    CHECK_EQ(dequeued.canId, 0x102U);
    CHECK(ring.pop(&dequeued));
    CHECK_EQ(dequeued.canId, 0x103U);
    CHECK(ring.pop(&dequeued));
    CHECK_EQ(dequeued.canId, 0x104U);
    CHECK(ring.isEmpty());
    CHECK(!ring.pop(&dequeued));
}

/**
 * @brief Verify pushOverwrite replaces the oldest frame and increments droppedCount.
 * @req REQ-RING-002
 */
void testRingBufferPushOverwrite() {
    FrameRingBuffer ring(2U);
    CHECK(!ring.pushOverwrite(makeSampleFrame(0x201U, 10U)));
    CHECK(!ring.pushOverwrite(makeSampleFrame(0x202U, 20U)));
    CHECK_EQ(ring.droppedCount(), static_cast<std::size_t>(0U));

    CHECK(ring.pushOverwrite(makeSampleFrame(0x203U, 30U)));
    CHECK_EQ(ring.droppedCount(), static_cast<std::size_t>(1U));
    CHECK_EQ(ring.size(), static_cast<std::size_t>(2U));

    CanFrame dequeued{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(ring.pop(&dequeued));
    CHECK_EQ(dequeued.canId, 0x202U);
    CHECK(ring.pop(&dequeued));
    CHECK_EQ(dequeued.canId, 0x203U);
    CHECK(ring.isEmpty());
}

/**
 * @brief Verify peek inspects the oldest frame without removing it and clear resets state.
 * @req REQ-RING-003
 */
void testRingBufferPeekAndClear() {
    FrameRingBuffer ring(4U);
    CanFrame inspected{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(!ring.peek(&inspected));
    CHECK(!ring.peek(nullptr));

    CHECK(ring.push(makeSampleFrame(0x301U, 11U)));
    CHECK(ring.push(makeSampleFrame(0x302U, 22U)));

    CHECK(ring.peek(&inspected));
    CHECK_EQ(inspected.canId, 0x301U);
    CHECK_EQ(ring.size(), static_cast<std::size_t>(2U));

    ring.clear();
    CHECK(ring.isEmpty());
    CHECK_EQ(ring.size(), static_cast<std::size_t>(0U));
    CHECK_EQ(ring.droppedCount(), static_cast<std::size_t>(0U));
}

}  // namespace

void runRingBufferTests() {
    testRingBufferFifoAndFullRejection();
    testRingBufferPushOverwrite();
    testRingBufferPeekAndClear();
}

}  // namespace can_fixture_test
