/**
 * @file voltage_decoder.hpp
 * @brief Non-throwing supply voltage decoder returning explicit status and value.
 */

#ifndef VOLTAGE_DECODER_HPP
#define VOLTAGE_DECODER_HPP

#include <cstddef>
#include <cstdint>

/**
 * @brief Decoding outcome for supply voltage conversion.
 */
enum class DecodeStatus : std::uint8_t {
    /** @brief Input was valid and millivolts holds the decoded reading. */
    Ok = 0U,
    /** @brief Input pointer was null. */
    NullInput = 1U,
    /** @brief Input byte buffer contained fewer than two bytes. */
    InvalidLength = 2U,
    /** @brief Extracted 12-bit ADC count exceeded the valid range [0, 4095]. */
    OutOfRange = 3U
};

/**
 * @brief Status-paired optional result returned by decodeSupplyVoltage.
 */
struct DecodeResult {
    /** @brief Decoding status code. */
    DecodeStatus status;
    /** @brief Decoded voltage in millivolts when status is Ok; zero otherwise. */
    std::int32_t millivolts;
};

/**
 * @brief Decode a big-endian 12-bit ADC reading from a byte buffer into millivolts.
 * @param[in] rawBytes  Pointer to at least two raw input bytes.
 * @param[in] byteCount Number of valid bytes available at rawBytes.
 * @return DecodeResult with DecodeStatus::Ok and scaled millivolts (raw * 5), or error status with 0.
 */
[[nodiscard]] DecodeResult decodeSupplyVoltage(
    const std::uint8_t* rawBytes,
    std::size_t byteCount) noexcept;

#endif
