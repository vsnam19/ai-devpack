/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 13-vague-frame-retention (REQ-CODEC-002 / REQ-RING-002).
 */

#include <array>

#include "can_codec.hpp"
#include "ring_buffer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::CanFrame;
    using can_fixture::Endianness;
    using can_fixture::FrameRingBuffer;
    using can_fixture::kMaxFrameBytes;
    using can_fixture::SignalDef;

    // 1. Packing sub-byte signals into a 0xFF-filled frame preserves unrelated bits
    std::array<std::uint8_t, kMaxFrameBytes> allOnes{
        0xFFU, 0xFFU, 0xFFU, 0xFFU, 0xFFU, 0xFFU, 0xFFU, 0xFFU};
    const SignalDef lowNibble{0U, 4U, Endianness::Little, false, 1.0, 0.0, 0.0, 15.0};
    const SignalDef highNibble{4U, 4U, Endianness::Little, false, 1.0, 0.0, 0.0, 15.0};
    const SignalDef singleFlag{8U, 1U, Endianness::Little, false, 1.0, 0.0, 0.0, 1.0};
    const SignalDef sevenBitTail{56U, 7U, Endianness::Little, false, 1.0, 0.0, 0.0, 127.0};

    CHECK(can_fixture::packRaw(lowNibble, 0x5ULL, allOnes.data(), allOnes.size()));
    CHECK_EQ(allOnes[0], static_cast<std::uint8_t>(0xF5U));
    CHECK(can_fixture::packRaw(singleFlag, 0x0ULL, allOnes.data(), allOnes.size()));
    CHECK_EQ(allOnes[1], static_cast<std::uint8_t>(0xFEU));
    CHECK(can_fixture::packRaw(sevenBitTail, 0x2AULL, allOnes.data(), allOnes.size()));
    CHECK_EQ(allOnes[7], static_cast<std::uint8_t>(0xAAU));

    // 2. Reverse order: high nibble then low nibble in byte 0
    std::array<std::uint8_t, kMaxFrameBytes> zeroed{};
    CHECK(can_fixture::packRaw(highNibble, 0xCULL, zeroed.data(), zeroed.size()));
    CHECK(can_fixture::packRaw(lowNibble, 0x3ULL, zeroed.data(), zeroed.size()));
    CHECK_EQ(zeroed[0], static_cast<std::uint8_t>(0xC3U));

    // 3. Sustained burst retention with co-packed signals across pushOverwrite
    FrameRingBuffer ring(3U);
    for (std::uint32_t idx = 0U; idx < 6U; ++idx) {
        CanFrame frame{0x300U + idx, 8U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
        CHECK(can_fixture::packRaw(highNibble, idx + 1ULL, frame.payload, kMaxFrameBytes));
        CHECK(can_fixture::packRaw(lowNibble, (idx * 2ULL) & 0xFULL, frame.payload, kMaxFrameBytes));
        const bool expectedOverwrite = (idx >= 3U);
        CHECK_EQ(ring.pushOverwrite(frame), expectedOverwrite);
    }
    CHECK_EQ(ring.size(), static_cast<std::size_t>(3U));
    CHECK_EQ(ring.droppedCount(), static_cast<std::size_t>(3U));

    for (std::uint32_t expectedIdx = 3U; expectedIdx < 6U; ++expectedIdx) {
        CanFrame popped{0U, 0U, {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U}};
        CHECK(ring.pop(&popped));
        CHECK_EQ(popped.canId, 0x300U + expectedIdx);
        std::uint64_t highRaw = 0ULL;
        std::uint64_t lowRaw = 0ULL;
        CHECK(can_fixture::unpackRaw(highNibble, popped.payload, kMaxFrameBytes, &highRaw));
        CHECK(can_fixture::unpackRaw(lowNibble, popped.payload, kMaxFrameBytes, &lowRaw));
        CHECK_EQ(highRaw, static_cast<std::uint64_t>(expectedIdx + 1U));
        CHECK_EQ(lowRaw, static_cast<std::uint64_t>((expectedIdx * 2U) & 0xFU));
    }
    CHECK(ring.isEmpty());

    return can_fixture_test::printSummaryAndExitCode<0>();
}
