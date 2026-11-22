/**
 * @file test_can_codec.cpp
 * @brief Unit tests for CAN signal validation, packing, unpacking, sign extension, and scaling.
 */

#include <array>

#include "can_codec.hpp"
#include "test_framework.hpp"

namespace can_fixture_test {

namespace {

using can_fixture::Endianness;
using can_fixture::kMaxFrameBytes;
using can_fixture::SignalDef;

/**
 * @brief Verify signal layout and scaling parameter validation.
 * @req REQ-CODEC-001
 */
void testSignalValidation() {
    const SignalDef validSignal{0U, 16U, Endianness::Little, false, 0.5, -10.0, -10.0, 32757.5};
    CHECK(can_fixture::isValidSignal(validSignal));

    const SignalDef zeroLengthSignal{0U, 0U, Endianness::Little, false, 1.0, 0.0, 0.0, 1.0};
    CHECK(!can_fixture::isValidSignal(zeroLengthSignal));

    const SignalDef overflowBitSignal{57U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    CHECK(!can_fixture::isValidSignal(overflowBitSignal));

    const SignalDef exactBoundarySignal{56U, 8U, Endianness::Big, false, 1.0, 0.0, 0.0, 255.0};
    CHECK(can_fixture::isValidSignal(exactBoundarySignal));

    const SignalDef negativeScaleSignal{0U, 8U, Endianness::Little, false, -1.0, 0.0, 0.0, 255.0};
    CHECK(!can_fixture::isValidSignal(negativeScaleSignal));

    const SignalDef invertedBoundsSignal{0U, 8U, Endianness::Little, false, 1.0, 0.0, 50.0, 10.0};
    CHECK(!can_fixture::isValidSignal(invertedBoundsSignal));
}

/**
 * @brief Verify little-endian unsigned packing and unpacking across byte boundaries.
 * @req REQ-CODEC-002
 */
void testUnsignedLittleEndianPackUnpack() {
    std::array<std::uint8_t, kMaxFrameBytes> frameBytes{};
    const SignalDef lowByteSignal{0U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    const SignalDef crossByteSignal{12U, 12U, Endianness::Little, false, 1.0, 0.0, 0.0, 4095.0};

    CHECK(can_fixture::packRaw(lowByteSignal, 42ULL, frameBytes.data(), frameBytes.size()));
    CHECK(can_fixture::packRaw(crossByteSignal, 0xABCULL, frameBytes.data(), frameBytes.size()));

    std::uint64_t unpackedLow = 0ULL;
    std::uint64_t unpackedCross = 0ULL;
    CHECK(can_fixture::unpackRaw(lowByteSignal, frameBytes.data(), frameBytes.size(), &unpackedLow));
    CHECK(can_fixture::unpackRaw(crossByteSignal, frameBytes.data(), frameBytes.size(), &unpackedCross));
    CHECK_EQ(unpackedLow, 42ULL);
    CHECK_EQ(unpackedCross, 0xABCULL);
}

/**
 * @brief Verify big-endian packing and unpacking across byte boundaries.
 * @req REQ-CODEC-003
 */
void testBigEndianPackUnpack() {
    std::array<std::uint8_t, kMaxFrameBytes> frameBytes{};
    const SignalDef wordSignal{0U, 16U, Endianness::Big, false, 1.0, 0.0, 0.0, 65535.0};
    const SignalDef nibbleSpanSignal{20U, 12U, Endianness::Big, false, 1.0, 0.0, 0.0, 4095.0};

    CHECK(can_fixture::packRaw(wordSignal, 0x1234ULL, frameBytes.data(), frameBytes.size()));
    CHECK_EQ(frameBytes[0], static_cast<std::uint8_t>(0x12U));
    CHECK_EQ(frameBytes[1], static_cast<std::uint8_t>(0x34U));

    CHECK(can_fixture::packRaw(nibbleSpanSignal, 0x5A7ULL, frameBytes.data(), frameBytes.size()));

    std::uint64_t unpackedWord = 0ULL;
    std::uint64_t unpackedSpan = 0ULL;
    CHECK(can_fixture::unpackRaw(wordSignal, frameBytes.data(), frameBytes.size(), &unpackedWord));
    CHECK(can_fixture::unpackRaw(nibbleSpanSignal, frameBytes.data(), frameBytes.size(), &unpackedSpan));
    CHECK_EQ(unpackedWord, 0x1234ULL);
    CHECK_EQ(unpackedSpan, 0x5A7ULL);
}

/**
 * @brief Verify two's-complement sign extension and signed physical encode/decode.
 * @req REQ-CODEC-004
 */
void testSignedSignalEncodeDecode() {
    CHECK_EQ(can_fixture::signExtend(0xFFULL, 8U), static_cast<std::int64_t>(-1));
    CHECK_EQ(can_fixture::signExtend(0x80ULL, 8U), static_cast<std::int64_t>(-128));
    CHECK_EQ(can_fixture::signExtend(0x7FULL, 8U), static_cast<std::int64_t>(127));
    CHECK_EQ(can_fixture::signExtend(0xF80ULL, 12U), static_cast<std::int64_t>(-128));

    std::array<std::uint8_t, kMaxFrameBytes> frameBytes{};
    const SignalDef steeringAngleSignal{
        0U, 12U, Endianness::Little, true, 0.5, 0.0, -500.0, 500.0};

    CHECK(can_fixture::encodePhysical(steeringAngleSignal, -125.5, frameBytes.data(), frameBytes.size()));
    double decodedAngle = 0.0;
    CHECK(can_fixture::decodePhysical(
        steeringAngleSignal, frameBytes.data(), frameBytes.size(), &decodedAngle));
    CHECK_NEAR(decodedAngle, -125.5, 1e-6);
}

/**
 * @brief Verify physical scale, offset, and min/max clamping on encode and decode.
 * @req REQ-CODEC-005
 */
void testPhysicalScalingAndClamping() {
    std::array<std::uint8_t, kMaxFrameBytes> frameBytes{};
    const SignalDef coolantTempSignal{
        16U, 8U, Endianness::Little, false, 1.0, -40.0, -40.0, 150.0};

    CHECK(can_fixture::encodePhysical(coolantTempSignal, 95.0, frameBytes.data(), frameBytes.size()));
    double decodedTemp = 0.0;
    CHECK(can_fixture::decodePhysical(
        coolantTempSignal, frameBytes.data(), frameBytes.size(), &decodedTemp));
    CHECK_NEAR(decodedTemp, 95.0, 1e-6);

    CHECK(can_fixture::encodePhysical(coolantTempSignal, 220.0, frameBytes.data(), frameBytes.size()));
    CHECK(can_fixture::decodePhysical(
        coolantTempSignal, frameBytes.data(), frameBytes.size(), &decodedTemp));
    CHECK_NEAR(decodedTemp, 150.0, 1e-6);

    CHECK(can_fixture::encodePhysical(coolantTempSignal, -100.0, frameBytes.data(), frameBytes.size()));
    CHECK(can_fixture::decodePhysical(
        coolantTempSignal, frameBytes.data(), frameBytes.size(), &decodedTemp));
    CHECK_NEAR(decodedTemp, -40.0, 1e-6);
}

}  // namespace

void runCanCodecTests() {
    testSignalValidation();
    testUnsignedLittleEndianPackUnpack();
    testBigEndianPackUnpack();
    testSignedSignalEncodeDecode();
    testPhysicalScalingAndClamping();
}

}  // namespace can_fixture_test
