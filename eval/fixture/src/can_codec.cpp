/**
 * @file can_codec.cpp
 * @brief Implementation of CAN signal validation, bit packing/unpacking, and scaling.
 */

#include "can_codec.hpp"

#include <cmath>
#include <limits>

namespace can_fixture {

namespace {

/**
 * @brief Compute the bit mask for a given bitLength in [1, 64].
 * @param[in] bitLength Number of bits (1..64).
 * @return Unsigned 64-bit mask with the lowest bitLength bits set to 1.
 */
std::uint64_t computeBitMask(std::uint8_t bitLength) noexcept {
    if (bitLength >= static_cast<std::uint8_t>(kMaxFrameBits)) {
        return std::numeric_limits<std::uint64_t>::max();
    }
    return (1ULL << bitLength) - 1ULL;
}

/**
 * @brief Map a signal bit index (0 = LSB) to (byteIndex, bitInByte) in the frame array.
 * @param[in] signal Signal layout descriptor.
 * @param[in] signalBitIndex Bit position within the signal (0 = LSB, bitLength - 1 = MSB).
 * @param[out] byteIndexOut Target byte index in the 8-byte frame (0..7).
 * @param[out] bitInByteOut Target bit significance within the byte (0 = LSB .. 7 = MSB).
 */
void resolveFrameBitPosition(
    const SignalDef& signal,
    std::uint8_t signalBitIndex,
    std::size_t* byteIndexOut,
    std::uint8_t* bitInByteOut) noexcept {
    if (signal.endianness == Endianness::Little) {
        const std::size_t frameBit =
            static_cast<std::size_t>(signal.startBit) + static_cast<std::size_t>(signalBitIndex);
        *byteIndexOut = frameBit / 8U;
        *bitInByteOut = static_cast<std::uint8_t>(frameBit % 8U);
    } else {
        const auto offsetFromMsb =
            static_cast<std::size_t>(signal.bitLength - 1U - signalBitIndex);
        const std::size_t networkBit =
            static_cast<std::size_t>(signal.startBit) + offsetFromMsb;
        *byteIndexOut = networkBit / 8U;
        *bitInByteOut = static_cast<std::uint8_t>(7U - (networkBit % 8U));
    }
}

}  // namespace

bool isValidSignal(const SignalDef& signal) noexcept {
    if (signal.bitLength == 0U || signal.bitLength > static_cast<std::uint8_t>(kMaxFrameBits)) {
        return false;
    }
    const std::size_t endBit =
        static_cast<std::size_t>(signal.startBit) + static_cast<std::size_t>(signal.bitLength);
    if (endBit > kMaxFrameBits) {
        return false;
    }
    if (signal.endianness != Endianness::Little && signal.endianness != Endianness::Big) {
        return false;
    }
    if (!std::isfinite(signal.scale) || signal.scale <= 0.0) {
        return false;
    }
    if (!std::isfinite(signal.offset) || !std::isfinite(signal.minPhysical) ||
        !std::isfinite(signal.maxPhysical)) {
        return false;
    }
    if (signal.minPhysical > signal.maxPhysical) {
        return false;
    }
    return true;
}

bool packRaw(
    const SignalDef& signal,
    std::uint64_t rawValue,
    std::uint8_t* frameBytes,
    std::size_t frameLength) noexcept {
    if (!isValidSignal(signal) || frameBytes == nullptr || frameLength < kMaxFrameBytes) {
        return false;
    }
    const std::uint64_t maskedValue = rawValue & computeBitMask(signal.bitLength);
    for (std::uint8_t i = 0U; i < signal.bitLength; ++i) {
        std::size_t byteIndex = 0U;
        std::uint8_t bitInByte = 0U;
        resolveFrameBitPosition(signal, i, &byteIndex, &bitInByte);
        const auto bitMask = static_cast<std::uint8_t>(1U << bitInByte);
        const bool bitIsSet = ((maskedValue >> i) & 1ULL) != 0ULL;
        if (bitIsSet) {
            frameBytes[byteIndex] = static_cast<std::uint8_t>(frameBytes[byteIndex] | bitMask);
        } else {
            frameBytes[byteIndex] =
                static_cast<std::uint8_t>(frameBytes[byteIndex] & static_cast<std::uint8_t>(~bitMask));
        }
    }
    return true;
}

bool unpackRaw(
    const SignalDef& signal,
    const std::uint8_t* frameBytes,
    std::size_t frameLength,
    std::uint64_t* rawOut) noexcept {
    if (!isValidSignal(signal) || frameBytes == nullptr || rawOut == nullptr ||
        frameLength < kMaxFrameBytes) {
        return false;
    }
    std::uint64_t extractedBits = 0ULL;
    for (std::uint8_t i = 0U; i < signal.bitLength; ++i) {
        std::size_t byteIndex = 0U;
        std::uint8_t bitInByte = 0U;
        resolveFrameBitPosition(signal, i, &byteIndex, &bitInByte);
        const std::uint8_t byteValue = frameBytes[byteIndex];
        if (((byteValue >> bitInByte) & 1U) != 0U) {
            extractedBits |= (1ULL << i);
        }
    }
    *rawOut = extractedBits;
    return true;
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
std::int64_t signExtend(std::uint64_t rawValue, std::uint8_t bitLength) noexcept {
    if (bitLength == 0U) {
        return 0;
    }
    if (bitLength >= static_cast<std::uint8_t>(kMaxFrameBits)) {
        return static_cast<std::int64_t>(rawValue);
    }
    const std::uint64_t mask = computeBitMask(bitLength);
    std::uint64_t cleanedBits = rawValue & mask;
    const std::uint64_t signBitMask = 1ULL << (bitLength - 1U);
    if ((cleanedBits & signBitMask) != 0ULL) {
        cleanedBits |= ~mask;
    }
    return static_cast<std::int64_t>(cleanedBits);
}

bool encodePhysical(
    const SignalDef& signal,
    double physicalValue,
    std::uint8_t* frameBytes,
    std::size_t frameLength) noexcept {
    if (!isValidSignal(signal) || frameBytes == nullptr || frameLength < kMaxFrameBytes ||
        !std::isfinite(physicalValue)) {
        return false;
    }
    double clampedPhysical = physicalValue;
    if (clampedPhysical < signal.minPhysical) {
        clampedPhysical = signal.minPhysical;
    } else if (clampedPhysical > signal.maxPhysical) {
        clampedPhysical = signal.maxPhysical;
    }

    const double scaledReal = std::round((clampedPhysical - signal.offset) / signal.scale);
    std::uint64_t rawToPack = 0ULL;

    if (signal.isSigned) {
        std::int64_t minSigned = std::numeric_limits<std::int64_t>::min();
        std::int64_t maxSigned = std::numeric_limits<std::int64_t>::max();
        if (signal.bitLength < static_cast<std::uint8_t>(kMaxFrameBits)) {
            minSigned = -(1LL << (signal.bitLength - 1U));
            maxSigned = (1LL << (signal.bitLength - 1U)) - 1LL;
        }
        std::int64_t clampedSigned = 0;
        if (scaledReal <= static_cast<double>(minSigned)) {
            clampedSigned = minSigned;
        } else if (scaledReal >= static_cast<double>(maxSigned)) {
            clampedSigned = maxSigned;
        } else {
            clampedSigned = static_cast<std::int64_t>(scaledReal);
        }
        rawToPack = static_cast<std::uint64_t>(clampedSigned) & computeBitMask(signal.bitLength);
    } else {
        const std::uint64_t maxUnsigned = computeBitMask(signal.bitLength);
        if (scaledReal <= 0.0) {
            rawToPack = 0ULL;
        } else if (scaledReal >= static_cast<double>(maxUnsigned)) {
            rawToPack = maxUnsigned;
        } else {
            rawToPack = static_cast<std::uint64_t>(scaledReal);
        }
    }

    return packRaw(signal, rawToPack, frameBytes, frameLength);
}

bool decodePhysical(
    const SignalDef& signal,
    const std::uint8_t* frameBytes,
    std::size_t frameLength,
    double* physicalOut) noexcept {
    if (physicalOut == nullptr) {
        return false;
    }
    std::uint64_t rawValue = 0ULL;
    if (!unpackRaw(signal, frameBytes, frameLength, &rawValue)) {
        return false;
    }
    double rawNumeric = 0.0;
    if (signal.isSigned) {
        rawNumeric = static_cast<double>(signExtend(rawValue, signal.bitLength));
    } else {
        rawNumeric = static_cast<double>(rawValue);
    }
    double decodedValue = (rawNumeric * signal.scale) + signal.offset;
    if (decodedValue < signal.minPhysical) {
        decodedValue = signal.minPhysical;
    } else if (decodedValue > signal.maxPhysical) {
        decodedValue = signal.maxPhysical;
    }
    *physicalOut = decodedValue;
    return true;
}

}  // namespace can_fixture
