#include "voltage_decoder.hpp"

#include <stdexcept>

DecodeResult decodeSupplyVoltage(const std::uint8_t* rawBytes, std::size_t byteCount)
{
    if (rawBytes == nullptr) {
        throw std::invalid_argument("null input");
    }
    if (byteCount < 2U) {
        throw std::invalid_argument("short buffer");
    }

    std::uint16_t rawCount = (std::uint16_t)(((std::uint16_t)rawBytes[0] << 8U) | rawBytes[1]);
    if (rawCount > 4095U) {
        throw std::out_of_range("adc count out of range");
    }

    DecodeResult result;
    result.status = DecodeStatus::Ok;
    result.millivolts = (std::int32_t)rawCount * 5;
    return result;
}
