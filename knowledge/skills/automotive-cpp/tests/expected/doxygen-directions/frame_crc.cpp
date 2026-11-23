/**
 * @file frame_crc.cpp
 * @brief Bounded CRC-16 frame accumulator implementation.
 */

#include "frame_crc.hpp"

namespace {

constexpr std::size_t kMaxFrameLength = 64U;
constexpr std::uint16_t kCrcPolynomial = 0x1021U;

}  // namespace

bool updateFrameCrc(
    std::uint16_t& crcAccumulator,
    const std::uint8_t* const frameBytes,
    const std::size_t byteCount,
    bool& isValidOut) noexcept
{
    bool updated{false};
    isValidOut = false;

    if ((frameBytes != nullptr) && (byteCount > 0U) && (byteCount <= kMaxFrameLength)) {
        std::uint16_t workingCrc{crcAccumulator};
        for (std::size_t i = 0U; i < byteCount; ++i) {
            workingCrc = static_cast<std::uint16_t>(
                workingCrc ^ static_cast<std::uint16_t>(static_cast<std::uint16_t>(frameBytes[i]) << 8U));
            for (std::uint8_t j = 0U; j < 8U; ++j) {
                if ((workingCrc & 0x8000U) != 0U) {
                    workingCrc = static_cast<std::uint16_t>((workingCrc << 1U) ^ kCrcPolynomial);
                } else {
                    workingCrc = static_cast<std::uint16_t>(workingCrc << 1U);
                }
            }
        }
        crcAccumulator = workingCrc;
        isValidOut = true;
        updated = true;
    }

    return updated;
}
