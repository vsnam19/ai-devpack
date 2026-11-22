/**
 * @file hidden_test.cpp
 * @brief Hidden tests for memory sequence 03-distractors-overrecall.
 */

#include "ring_buffer.hpp"
#include "test_framework.hpp"

int main() {
    can_fixture::FrameRingBuffer ring(4U);
    CHECK_EQ(ring.priorityCanIdTag(), static_cast<std::uint32_t>(0x7DFU));
    return can_fixture_test::printSummaryAndExitCode();
}
