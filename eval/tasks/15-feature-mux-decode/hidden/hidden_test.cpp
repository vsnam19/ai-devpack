/**
 * @file hidden_test.cpp
 * @brief Hidden boundary tests for task 15-feature-mux-decode (REQ-CODEC-008).
 */

#include <array>

#include "can_codec.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::Endianness;
    using can_fixture::kMaxFrameBytes;
    using can_fixture::SignalDef;

    const SignalDef mux8{0U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    const SignalDef mux16Big{0U, 16U, Endianness::Big, false, 1.0, 0.0, 0.0, 65535.0};
    const SignalDef valueSigned{8U, 12U, Endianness::Little, true, 0.25, -10.0, -50.0, 50.0};
    const SignalDef valueClamped{16U, 8U, Endianness::Big, false, 1.0, 0.0, 10.0, 100.0};

    // 1. Successful match with signed value signal
    std::array<std::uint8_t, kMaxFrameBytes> frame{};
    CHECK(can_fixture::packRaw(mux8, 3ULL, frame.data(), frame.size()));
    CHECK(can_fixture::encodePhysical(valueSigned, -12.5, frame.data(), frame.size()));

    double decoded = 999.0;
    CHECK(can_fixture::decodeMultiplexedPhysical(mux8, 3ULL, valueSigned, frame.data(), &decoded));
    CHECK_NEAR(decoded, -12.5, 1e-6);

    // 2. Selector mismatch leaves *physicalOut unmodified
    double sentinel = -42.125;
    CHECK(!can_fixture::decodeMultiplexedPhysical(mux8, 2ULL, valueSigned, frame.data(), &sentinel));
    CHECK_NEAR(sentinel, -42.125, 1e-9);

    // 3. Boundary: 16-bit big-endian mux selector succeeds and applies clamping on valueSignal
    std::array<std::uint8_t, kMaxFrameBytes> frame16{};
    CHECK(can_fixture::packRaw(mux16Big, 0xA55AULL, frame16.data(), frame16.size()));
    CHECK(can_fixture::packRaw(valueClamped, 200ULL, frame16.data(), frame16.size()));
    decoded = 0.0;
    CHECK(can_fixture::decodeMultiplexedPhysical(
        mux16Big, 0xA55AULL, valueClamped, frame16.data(), &decoded));
    CHECK_NEAR(decoded, 100.0, 1e-6);

    // 4. Boundary: 17-bit mux selector must be rejected without modifying output
    const SignalDef mux17{0U, 17U, Endianness::Little, false, 1.0, 0.0, 0.0, 131071.0};
    sentinel = 77.5;
    CHECK(!can_fixture::decodeMultiplexedPhysical(mux17, 0ULL, valueClamped, frame16.data(), &sentinel));
    CHECK_NEAR(sentinel, 77.5, 1e-9);

    // 5. Signed mux selector must be rejected without modifying output
    const SignalDef signedMux{0U, 8U, Endianness::Little, true, 1.0, 0.0, -128.0, 127.0};
    sentinel = 11.25;
    CHECK(!can_fixture::decodeMultiplexedPhysical(signedMux, 3ULL, valueSigned, frame.data(), &sentinel));
    CHECK_NEAR(sentinel, 11.25, 1e-9);

    // 6. Invalid selector or invalid value signal rejected without modifying output
    const SignalDef invalidMux{60U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    const SignalDef invalidValue{8U, 8U, Endianness::Little, false, 0.0, 0.0, 0.0, 255.0};
    sentinel = 55.0;
    CHECK(!can_fixture::decodeMultiplexedPhysical(invalidMux, 3ULL, valueSigned, frame.data(), &sentinel));
    CHECK_NEAR(sentinel, 55.0, 1e-9);
    CHECK(!can_fixture::decodeMultiplexedPhysical(mux8, 3ULL, invalidValue, frame.data(), &sentinel));
    CHECK_NEAR(sentinel, 55.0, 1e-9);

    // 7. Null frameBytes or null physicalOut rejected
    CHECK(!can_fixture::decodeMultiplexedPhysical(mux8, 3ULL, valueSigned, nullptr, &sentinel));
    CHECK_NEAR(sentinel, 55.0, 1e-9);
    CHECK(!can_fixture::decodeMultiplexedPhysical(mux8, 3ULL, valueSigned, frame.data(), nullptr));

    return can_fixture_test::printSummaryAndExitCode<0>();
}
