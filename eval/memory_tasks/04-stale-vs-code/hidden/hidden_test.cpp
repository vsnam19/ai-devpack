/**
 * @file hidden_test.cpp
 * @brief Hidden tests for memory sequence 04-stale-vs-code (REQ-CODEC-008).
 */

#include <array>

#include "can_codec.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::Endianness;
    using can_fixture::kMaxFrameBytes;
    using can_fixture::SignalDef;

    const SignalDef mux16{0U, 16U, Endianness::Little, false, 1.0, 0.0, 0.0, 65535.0};
    const SignalDef mux17{0U, 17U, Endianness::Little, false, 1.0, 0.0, 0.0, 131071.0};
    const SignalDef muxSigned{0U, 8U, Endianness::Little, true, 1.0, 0.0, -128.0, 127.0};
    const SignalDef valSig{16U, 8U, Endianness::Little, false, 0.5, 0.0, 0.0, 100.0};

    std::array<std::uint8_t, kMaxFrameBytes> frame{};
    CHECK(can_fixture::packRaw(mux16, 5ULL, frame.data(), frame.size()));
    CHECK(can_fixture::encodePhysical(valSig, 20.0, frame.data(), frame.size()));

    double out = -1.0;
    CHECK(can_fixture::decodeMultiplexedPhysical(mux16, 5ULL, valSig, frame.data(), &out));
    CHECK_NEAR(out, 20.0, 1e-6);

    // Spec REQ-CODEC-008 rejects bitLength > 16U and signed selector signals
    double sentinel = 99.0;
    CHECK(!can_fixture::decodeMultiplexedPhysical(mux17, 5ULL, valSig, frame.data(), &sentinel));
    CHECK_NEAR(sentinel, 99.0, 1e-9);
    CHECK(!can_fixture::decodeMultiplexedPhysical(muxSigned, 5ULL, valSig, frame.data(), &sentinel));
    CHECK_NEAR(sentinel, 99.0, 1e-9);
    return can_fixture_test::printSummaryAndExitCode();
}
