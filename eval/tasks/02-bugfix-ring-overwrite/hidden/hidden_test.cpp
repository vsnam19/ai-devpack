/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 02-bugfix-ring-overwrite.
 */

#include "ring_buffer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::CanFrame;
    using can_fixture::FrameRingBuffer;

    FrameRingBuffer singleSlot(1U);
    const CanFrame firstFrame{0x10U, 1U, {1U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    const CanFrame secondFrame{0x20U, 1U, {2U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(!singleSlot.pushOverwrite(firstFrame));
    CHECK(singleSlot.pushOverwrite(secondFrame));
    CHECK_EQ(singleSlot.size(), static_cast<std::size_t>(1U));
    CHECK_EQ(singleSlot.droppedCount(), static_cast<std::size_t>(1U));

    CanFrame outFrame{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(singleSlot.pop(&outFrame));
    CHECK_EQ(outFrame.canId, 0x20U);
    CHECK(singleSlot.isEmpty());

    FrameRingBuffer multiSlot(4U);
    for (std::uint32_t i = 0U; i < 25U; ++i) {
        const CanFrame frame{0x100U + i, 8U, {static_cast<std::uint8_t>(i), 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
        const bool dropped = multiSlot.pushOverwrite(frame);
        if (i < 4U) {
            CHECK(!dropped);
        } else {
            CHECK(dropped);
        }
    }
    CHECK_EQ(multiSlot.size(), static_cast<std::size_t>(4U));
    CHECK_EQ(multiSlot.droppedCount(), static_cast<std::size_t>(21U));

    for (std::uint32_t expectedIdx = 21U; expectedIdx < 25U; ++expectedIdx) {
        CHECK(multiSlot.peek(&outFrame));
        CHECK_EQ(outFrame.canId, 0x100U + expectedIdx);
        CHECK(multiSlot.pop(&outFrame));
        CHECK_EQ(outFrame.canId, 0x100U + expectedIdx);
        CHECK_EQ(outFrame.payload[0], static_cast<std::uint8_t>(expectedIdx));
    }
    CHECK(multiSlot.isEmpty());

    return can_fixture_test::printSummaryAndExitCode<0>();
}
