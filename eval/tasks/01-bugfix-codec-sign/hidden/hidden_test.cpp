/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 01-bugfix-codec-sign.
 */

#include <limits>

#include "can_codec.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::Endianness;
    using can_fixture::kMaxFrameBytes;
    using can_fixture::SignalDef;

    CHECK_EQ(can_fixture::signExtend(0ULL, 0U), static_cast<std::int64_t>(0));
    CHECK_EQ(can_fixture::signExtend(1ULL, 1U), static_cast<std::int64_t>(-1));
    CHECK_EQ(can_fixture::signExtend(0ULL, 1U), static_cast<std::int64_t>(0));

    CHECK_EQ(can_fixture::signExtend(0x8ULL, 4U), static_cast<std::int64_t>(-8));
    CHECK_EQ(can_fixture::signExtend(0xFULL, 4U), static_cast<std::int64_t>(-1));
    CHECK_EQ(can_fixture::signExtend(0x7ULL, 4U), static_cast<std::int64_t>(7));

    CHECK_EQ(can_fixture::signExtend(0x800ULL, 12U), static_cast<std::int64_t>(-2048));
    CHECK_EQ(can_fixture::signExtend(0xFFFULL, 12U), static_cast<std::int64_t>(-1));
    CHECK_EQ(can_fixture::signExtend(0x7FFULL, 12U), static_cast<std::int64_t>(2047));

    CHECK_EQ(can_fixture::signExtend(0x8000ULL, 16U), static_cast<std::int64_t>(-32768));
    CHECK_EQ(can_fixture::signExtend(0x80000000ULL, 32U), static_cast<std::int64_t>(-2147483648LL));
    CHECK_EQ(
        can_fixture::signExtend(0xFFFFFFFFFFFFFFFFULL, 64U),
        static_cast<std::int64_t>(-1));

    std::uint8_t frameBytes[kMaxFrameBytes] = {0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U};
    const SignalDef signedBigSignal{8U, 12U, Endianness::Big, true, 0.25, -5.0, -600.0, 600.0};
    CHECK(can_fixture::encodePhysical(signedBigSignal, -130.25, frameBytes, kMaxFrameBytes));
    double decoded = 0.0;
    CHECK(can_fixture::decodePhysical(signedBigSignal, frameBytes, kMaxFrameBytes, &decoded));
    CHECK_NEAR(decoded, -130.25, 1e-6);

    return can_fixture_test::printSummaryAndExitCode<0>();
}
