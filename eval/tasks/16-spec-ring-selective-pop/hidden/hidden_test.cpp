/**
 * @file hidden_test.cpp
 * @brief Hidden boundary tests for task 16-spec-ring-selective-pop (REQ-RING-005).
 */

#include "ring_buffer.hpp"
#include "test_framework.hpp"

namespace {

/**
 * @brief Create a deterministic CanFrame with distinct payload bytes.
 * @param[in] canId Frame identifier.
 * @param[in] tag Seed byte encoded across payload bytes.
 * @return Populated CanFrame.
 */
can_fixture::CanFrame makeTaggedFrame(std::uint32_t canId, std::uint8_t tag) noexcept {
    can_fixture::CanFrame frame{
        canId,
        8U,
        {tag, static_cast<std::uint8_t>(tag + 1U), static_cast<std::uint8_t>(tag + 2U),
         0U, 0U, 0U, 0U, static_cast<std::uint8_t>(tag ^ 0xFFU)}};
    return frame;
}

}  // namespace

int main() {
    using can_fixture::CanFrame;
    using can_fixture::FrameRingBuffer;

    // 1. Empty buffer and null output pointer guards
    FrameRingBuffer ring(4U);
    CanFrame out{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(!ring.popByCanId(0x100U, &out));
    CHECK(!ring.popByCanId(0x100U, nullptr));

    // 2. Non-matching canId leaves state and droppedCount untouched
    CHECK(ring.push(makeTaggedFrame(0x101U, 10U)));
    CHECK(ring.push(makeTaggedFrame(0x102U, 20U)));
    out.canId = 0x999U;
    CHECK(!ring.popByCanId(0x777U, &out));
    CHECK_EQ(out.canId, 0x999U);
    CHECK_EQ(ring.size(), static_cast<std::size_t>(2U));
    CHECK_EQ(ring.droppedCount(), static_cast<std::size_t>(0U));
    CHECK(!ring.popByCanId(0x101U, nullptr));
    CHECK_EQ(ring.size(), static_cast<std::size_t>(2U));

    // 3. Wrap-around compaction and first-match FIFO order for duplicate IDs
    CHECK(ring.push(makeTaggedFrame(0x103U, 30U)));
    CHECK(ring.push(makeTaggedFrame(0x104U, 40U)));
    CHECK(ring.isFull());

    // Advance tail across array boundary
    CHECK(ring.pop(&out));
    CHECK_EQ(out.canId, 0x101U);
    CHECK(ring.pop(&out));
    CHECK_EQ(out.canId, 0x102U);

    // Push wrapped elements: logical FIFO order is [0x103(30), 0x104(40), 0x200(50), 0x200(60)]
    CHECK(ring.push(makeTaggedFrame(0x200U, 50U)));
    CHECK(ring.push(makeTaggedFrame(0x200U, 60U)));
    CHECK(ring.isFull());

    // Extract middle-tail item (0x104)
    CHECK(ring.popByCanId(0x104U, &out));
    CHECK_EQ(out.canId, 0x104U);
    CHECK_EQ(out.payload[0], static_cast<std::uint8_t>(40U));
    CHECK_EQ(out.payload[7], static_cast<std::uint8_t>(40U ^ 0xFFU));
    CHECK_EQ(ring.size(), static_cast<std::size_t>(3U));
    CHECK_EQ(ring.droppedCount(), static_cast<std::size_t>(0U));

    // Push again after selective pop -> [0x103(30), 0x200(50), 0x200(60), 0x300(70)]
    CHECK(ring.push(makeTaggedFrame(0x300U, 70U)));
    CHECK(ring.isFull());

    // Extract first duplicate (0x200 with tag 50)
    CHECK(ring.popByCanId(0x200U, &out));
    CHECK_EQ(out.canId, 0x200U);
    CHECK_EQ(out.payload[0], static_cast<std::uint8_t>(50U));
    CHECK_EQ(ring.size(), static_cast<std::size_t>(3U));

    // Remaining FIFO must be [0x103(30), 0x200(60), 0x300(70)]
    CHECK(ring.pop(&out));
    CHECK_EQ(out.canId, 0x103U);
    CHECK_EQ(out.payload[0], static_cast<std::uint8_t>(30U));
    CHECK(ring.pop(&out));
    CHECK_EQ(out.canId, 0x200U);
    CHECK_EQ(out.payload[0], static_cast<std::uint8_t>(60U));
    CHECK(ring.pop(&out));
    CHECK_EQ(out.canId, 0x300U);
    CHECK_EQ(out.payload[0], static_cast<std::uint8_t>(70U));
    CHECK(ring.isEmpty());

    // 4. Capacity-1 edge case
    FrameRingBuffer tiny(1U);
    CHECK(tiny.push(makeTaggedFrame(0x555U, 9U)));
    CHECK(!tiny.popByCanId(0x556U, &out));
    CHECK(tiny.popByCanId(0x555U, &out));
    CHECK_EQ(out.canId, 0x555U);
    CHECK(tiny.isEmpty());

    return can_fixture_test::printSummaryAndExitCode<0>();
}
