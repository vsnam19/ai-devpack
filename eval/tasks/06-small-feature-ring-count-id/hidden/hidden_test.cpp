/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 06-small-feature-ring-count-id.
 */

#include "ring_buffer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::CanFrame;
    using can_fixture::FrameRingBuffer;

    FrameRingBuffer ring(4U);
    CHECK_EQ(ring.countByCanId(0x100U), static_cast<std::size_t>(0U));

    const CanFrame frameA1{0x100U, 8U, {1U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    const CanFrame frameB1{0x200U, 8U, {2U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    const CanFrame frameA2{0x100U, 8U, {3U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    const CanFrame frameC1{0x300U, 8U, {4U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};

    CHECK(ring.push(frameA1));
    CHECK(ring.push(frameB1));
    CHECK(ring.push(frameA2));
    CHECK_EQ(ring.countByCanId(0x100U), static_cast<std::size_t>(2U));
    CHECK_EQ(ring.countByCanId(0x200U), static_cast<std::size_t>(1U));
    CHECK_EQ(ring.countByCanId(0x999U), static_cast<std::size_t>(0U));

    CanFrame popped{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(ring.pop(&popped));
    CHECK_EQ(popped.canId, 0x100U);
    CHECK_EQ(ring.countByCanId(0x100U), static_cast<std::size_t>(1U));

    // Wrap around storage indices
    CHECK(ring.push(frameC1));
    CHECK(ring.push(frameA1));
    CHECK(ring.isFull());
    CHECK_EQ(ring.countByCanId(0x100U), static_cast<std::size_t>(2U));
    CHECK_EQ(ring.countByCanId(0x200U), static_cast<std::size_t>(1U));
    CHECK_EQ(ring.countByCanId(0x300U), static_cast<std::size_t>(1U));

    // Overwrite oldest (0x200U) with another 0x100U
    CHECK(ring.pushOverwrite(frameA2));
    CHECK_EQ(ring.countByCanId(0x200U), static_cast<std::size_t>(0U));
    CHECK_EQ(ring.countByCanId(0x100U), static_cast<std::size_t>(3U));
    CHECK_EQ(ring.size(), static_cast<std::size_t>(4U));

    return can_fixture_test::printSummaryAndExitCode<0>();
}
