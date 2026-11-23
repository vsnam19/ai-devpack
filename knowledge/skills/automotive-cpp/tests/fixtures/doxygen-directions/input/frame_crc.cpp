#include "frame_crc.hpp"

bool updateFrameCrc(
    std::uint16_t& crcAccumulator,
    const std::uint8_t* buf,
    std::size_t length,
    bool& isValidOut)
{
    if (buf == nullptr || length == 0U || length > 64U) {
        isValidOut = false;
        return false;
    }

    std::uint16_t workingCrc = crcAccumulator;
    for (std::size_t i = 0U; i < length; ++i) {
        workingCrc ^= static_cast<std::uint16_t>(buf[i]) << 8U;
        for (std::uint8_t j = 0U; j < 8U; ++j) {
            if ((workingCrc & 0x8000U) != 0U) {
                workingCrc = static_cast<std::uint16_t>((workingCrc << 1U) ^ 0x1021U);
            } else {
                workingCrc = static_cast<std::uint16_t>(workingCrc << 1U);
            }
        }
    }
    crcAccumulator = workingCrc;
    isValidOut = true;
    return true;
}
