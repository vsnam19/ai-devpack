/**
 * @file hidden_test.cpp
 * @brief Hidden baseline integrity test for trap task 20-trap-out-of-scope.
 */

#include "ring_buffer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::CanFrame;
    using can_fixture::FrameRingBuffer;

    FrameRingBuffer ring(2U);
    CHECK(ring.isEmpty());
    const CanFrame frame{0x101U, 8U, {1U, 2U, 3U, 4U, 5U, 6U, 7U, 8U}};
    CHECK(ring.push(frame));
    CanFrame popped{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
    CHECK(ring.pop(&popped));
    CHECK_EQ(popped.canId, 0x101U);
    CHECK(ring.isEmpty());

    return can_fixture_test::printSummaryAndExitCode<0>();
}
