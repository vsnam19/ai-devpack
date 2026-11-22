/**
 * @file can_codec.hpp
 * @brief CAN frame signal packing, unpacking, sign extension, and physical scaling API.
 */

#ifndef CAN_FIXTURE_CAN_CODEC_HPP
#define CAN_FIXTURE_CAN_CODEC_HPP

#include <cstddef>
#include <cstdint>

namespace can_fixture {

/**
 * @brief Maximum number of payload bytes in a classical CAN frame.
 */
constexpr std::size_t kMaxFrameBytes = 8U;

/**
 * @brief Maximum number of payload bits in a classical CAN frame.
 */
constexpr std::size_t kMaxFrameBits = 64U;

/**
 * @brief Bit ordering convention for a CAN signal within an 8-byte payload.
 */
enum class Endianness : std::uint8_t {
    /**
     * @brief Little-endian layout: signal bit k (0 = LSB) resides at frame bit (startBit + k),
     *        where frame bit pos maps to byteIndex = pos / 8 and bitInByte = pos % 8.
     */
    Little = 0U,

    /**
     * @brief Big-endian layout: signal bit offset m from MSB (0 = MSB, bitLength - 1 = LSB)
     *        resides at network bit pos = (startBit + m), where pos maps to
     *        byteIndex = pos / 8 and bitInByte = 7 - (pos % 8).
     */
    Big = 1U
};

/**
 * @brief Specification of a single CAN signal within a 64-bit frame payload.
 */
struct SignalDef {
    /** @brief Starting bit index in the 64-bit frame (0..63). */
    std::uint8_t startBit;
    /** @brief Length of the signal in bits (1..64). */
    std::uint8_t bitLength;
    /** @brief Byte and bit ordering convention (Little or Big). */
    Endianness endianness;
    /** @brief True if the raw value uses two's-complement signed representation. */
    bool isSigned;
    /** @brief Multiplicative scaling factor from raw value to physical units (must be > 0). */
    double scale;
    /** @brief Additive offset in physical units after multiplying by scale. */
    double offset;
    /** @brief Minimum clamped physical value. */
    double minPhysical;
    /** @brief Maximum clamped physical value. */
    double maxPhysical;
};

/**
 * @brief Validate a CAN signal definition against frame bit bounds and scaling rules.
 * @param[in] signal Signal definition to validate.
 * @return True if bitLength is in [1, 64], startBit + bitLength <= 64, scale > 0,
 *         and minPhysical <= maxPhysical.
 */
bool isValidSignal(const SignalDef& signal) noexcept;

/**
 * @brief Pack the lowest bitLength bits of rawValue into an 8-byte CAN frame buffer.
 * @param[in] signal Validated signal layout descriptor.
 * @param[in] rawValue Raw bit pattern to insert (lowest signal.bitLength bits used).
 * @param[in,out] frameBytes Target frame byte array of at least kMaxFrameBytes bytes.
 * @param[in] frameLength Length of frameBytes in bytes (must be >= kMaxFrameBytes).
 * @return True on success, false if signal is invalid, frameBytes is null, or frameLength < 8.
 */
bool packRaw(
    const SignalDef& signal,
    std::uint64_t rawValue,
    std::uint8_t* frameBytes,
    std::size_t frameLength) noexcept;

/**
 * @brief Unpack a raw unsigned bit pattern from an 8-byte CAN frame buffer.
 * @param[in] signal Validated signal layout descriptor.
 * @param[in] frameBytes Source frame byte array of at least kMaxFrameBytes bytes.
 * @param[in] frameLength Length of frameBytes in bytes (must be >= kMaxFrameBytes).
 * @param[out] rawOut Destination pointer receiving the extracted unsigned raw bits.
 * @return True on success, false if signal is invalid, pointers are null, or frameLength < 8.
 */
bool unpackRaw(
    const SignalDef& signal,
    const std::uint8_t* frameBytes,
    std::size_t frameLength,
    std::uint64_t* rawOut) noexcept;

/**
 * @brief Sign-extend an unsigned bitLength-bit raw value using two's-complement rules.
 * @param[in] rawValue Extracted unsigned bit pattern.
 * @param[in] bitLength Number of significant bits in rawValue (1..64).
 * @return Signed 64-bit integer value after sign extension.
 */
std::int64_t signExtend(std::uint64_t rawValue, std::uint8_t bitLength) noexcept;

/**
 * @brief Encode a physical floating-point value into a CAN frame buffer using signal scaling.
 * @param[in] signal Signal layout and scaling definition.
 * @param[in] physicalValue Engineering value to clamp, scale, and pack.
 * @param[in,out] frameBytes Target frame byte array of at least kMaxFrameBytes bytes.
 * @param[in] frameLength Length of frameBytes in bytes (must be >= kMaxFrameBytes).
 * @return True on success, false if signal or frame buffer arguments are invalid.
 */
bool encodePhysical(
    const SignalDef& signal,
    double physicalValue,
    std::uint8_t* frameBytes,
    std::size_t frameLength) noexcept;

/**
 * @brief Decode a physical floating-point value from a CAN frame buffer using signal scaling.
 * @param[in] signal Signal layout and scaling definition.
 * @param[in] frameBytes Source frame byte array of at least kMaxFrameBytes bytes.
 * @param[in] frameLength Length of frameBytes in bytes (must be >= kMaxFrameBytes).
 * @param[out] physicalOut Destination pointer receiving the decoded and clamped physical value.
 * @return True on success, false if signal, frameBytes, or physicalOut are invalid.
 */
bool decodePhysical(
    const SignalDef& signal,
    const std::uint8_t* frameBytes,
    std::size_t frameLength,
    double* physicalOut) noexcept;

}  // namespace can_fixture

#endif  // CAN_FIXTURE_CAN_CODEC_HPP
