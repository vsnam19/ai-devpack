/**
 * @file voltage_decoder.cpp
 * @brief Non-throwing supply voltage decoder implementation.
 */

#include "voltage_decoder.hpp"

namespace {

constexpr std::size_t kMinFrameBytes = 2U;
constexpr std::uint16_t kMaxAdcCount = 4095U;
constexpr std::int32_t kMillivoltsPerCount = 5;

}  // namespace

DecodeResult decodeSupplyVoltage(
    const std::uint8_t* const rawBytes,
    const std::size_t byteCount) noexcept
{
    DecodeResult result{DecodeStatus::Ok, 0};

    if (rawBytes == nullptr) {
        result.status = DecodeStatus::NullInput;
    } else if (byteCount < kMinFrameBytes) {
        result.status = DecodeStatus::InvalidLength;
    } else {
        const std::uint16_t highByte{static_cast<std::uint16_t>(rawBytes[0])};
        const std::uint16_t lowByte{static_cast<std::uint16_t>(rawBytes[1])};
        const std::uint16_t rawCount{
            static_cast<std::uint16_t>(static_cast<std::uint16_t>(highByte << 8U) | lowByte)};
        if (rawCount > kMaxAdcCount) {
            result.status = DecodeStatus::OutOfRange;
        } else {
            result.millivolts = static_cast<std::int32_t>(rawCount) * kMillivoltsPerCount;
        }
    }

    return result;
}
