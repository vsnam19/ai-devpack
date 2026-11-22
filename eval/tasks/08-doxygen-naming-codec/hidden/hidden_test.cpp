/**
 * @file hidden_test.cpp
 * @brief Hidden grading test for task 08-doxygen-naming-codec.
 */

#include <array>

#include "can_codec.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::Endianness;
    using can_fixture::kMaxFrameBytes;
    using can_fixture::SignalDef;

    std::array<std::uint8_t, kMaxFrameBytes> frameBytes{};
    const SignalDef signal{4U, 12U, Endianness::Little, true, 0.5, -10.0, -500.0, 500.0};
    CHECK(can_fixture::isValidSignal(signal));
    CHECK(can_fixture::encodePhysical(signal, -45.5, frameBytes.data(), frameBytes.size()));

    double decoded = 0.0;
    CHECK(can_fixture::decodePhysical(signal, frameBytes.data(), frameBytes.size(), &decoded));
    CHECK_NEAR(decoded, -45.5, 1e-6);

    return can_fixture_test::printSummaryAndExitCode<0>();
}
