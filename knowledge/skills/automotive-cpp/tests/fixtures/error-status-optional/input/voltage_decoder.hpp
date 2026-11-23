#ifndef VOLTAGE_DECODER_HPP
#define VOLTAGE_DECODER_HPP

#include <cstddef>
#include <cstdint>

// Status codes for supply voltage decoding without exceptions.
enum class DecodeStatus : std::uint8_t {
    Ok = 0U,
    NullInput = 1U,
    InvalidLength = 2U,
    OutOfRange = 3U
};

// Optional-like result pairing DecodeStatus with the decoded millivolt value.
struct DecodeResult {
    DecodeStatus status;
    std::int32_t millivolts;
};

// Decode a big-endian 12-bit ADC count (2 bytes, 0..4095) into millivolts (raw * 5).
// Contract:
// - rawBytes == nullptr -> {DecodeStatus::NullInput, 0}
// - byteCount < 2U      -> {DecodeStatus::InvalidLength, 0}
// - rawCount > 4095U    -> {DecodeStatus::OutOfRange, 0}
// - valid rawCount      -> {DecodeStatus::Ok, static_cast<std::int32_t>(rawCount) * 5}
DecodeResult decodeSupplyVoltage(const std::uint8_t* rawBytes, std::size_t byteCount);

#endif
