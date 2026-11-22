/**
 * @file hidden_test.cpp
 * @brief Hidden grading test for task 04-add-test-codec-bounds.
 */

#include <array>

#include "can_codec.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::Endianness;
    using can_fixture::kMaxFrameBytes;
    using can_fixture::SignalDef;

    std::array<std::uint8_t, kMaxFrameBytes> frameBytes{};
    std::uint64_t rawOut = 0ULL;
    const SignalDef validSig{56U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    const SignalDef outOfBoundsSig{57U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};

    CHECK(can_fixture::isValidSignal(validSig));
    CHECK(!can_fixture::isValidSignal(outOfBoundsSig));
    CHECK(!can_fixture::packRaw(outOfBoundsSig, 1ULL, frameBytes.data(), frameBytes.size()));
    CHECK(!can_fixture::unpackRaw(outOfBoundsSig, frameBytes.data(), frameBytes.size(), &rawOut));
    CHECK(!can_fixture::packRaw(validSig, 1ULL, nullptr, kMaxFrameBytes));
    CHECK(!can_fixture::packRaw(validSig, 1ULL, frameBytes.data(), 7U));
    CHECK(!can_fixture::unpackRaw(validSig, nullptr, kMaxFrameBytes, &rawOut));
    CHECK(!can_fixture::unpackRaw(validSig, frameBytes.data(), kMaxFrameBytes, nullptr));
    CHECK(!can_fixture::unpackRaw(validSig, frameBytes.data(), 7U, &rawOut));

    return can_fixture_test::printSummaryAndExitCode<0>();
}
