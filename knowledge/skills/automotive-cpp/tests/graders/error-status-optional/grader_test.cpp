#include "voltage_decoder.hpp"

#include <array>
#include <cassert>
#include <cstdint>

int main()
{
    const DecodeResult nullResult = decodeSupplyVoltage(nullptr, 2U);
    assert(nullResult.status == DecodeStatus::NullInput);
    assert(nullResult.millivolts == 0);

    const std::array<std::uint8_t, 1U> shortBytes{0x01U};
    const DecodeResult emptyResult = decodeSupplyVoltage(shortBytes.data(), 0U);
    assert(emptyResult.status == DecodeStatus::InvalidLength);
    assert(emptyResult.millivolts == 0);

    const DecodeResult oneByteResult = decodeSupplyVoltage(shortBytes.data(), 1U);
    assert(oneByteResult.status == DecodeStatus::InvalidLength);
    assert(oneByteResult.millivolts == 0);

    const std::array<std::uint8_t, 2U> zeroBytes{0x00U, 0x00U};
    const DecodeResult zeroResult = decodeSupplyVoltage(zeroBytes.data(), zeroBytes.size());
    assert(zeroResult.status == DecodeStatus::Ok);
    assert(zeroResult.millivolts == 0);

    // 0x03E8 = 1000 counts -> 5000 mV
    const std::array<std::uint8_t, 2U> midBytes{0x03U, 0xE8U};
    const DecodeResult midResult = decodeSupplyVoltage(midBytes.data(), midBytes.size());
    assert(midResult.status == DecodeStatus::Ok);
    assert(midResult.millivolts == 5000);

    // 0x0FFF = 4095 counts -> 20475 mV
    const std::array<std::uint8_t, 2U> maxBytes{0x0FU, 0xFFU};
    const DecodeResult maxResult = decodeSupplyVoltage(maxBytes.data(), maxBytes.size());
    assert(maxResult.status == DecodeStatus::Ok);
    assert(maxResult.millivolts == 20475);

    // 0x1000 = 4096 counts -> OutOfRange
    const std::array<std::uint8_t, 2U> overBytes{0x10U, 0x00U};
    const DecodeResult overResult = decodeSupplyVoltage(overBytes.data(), overBytes.size());
    assert(overResult.status == DecodeStatus::OutOfRange);
    assert(overResult.millivolts == 0);

    return 0;
}
