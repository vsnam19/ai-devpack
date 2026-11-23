#include "voltage_decoder.hpp"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

#ifdef GRADER_CHECK
#error "Candidate header must not define GRADER_CHECK"
#endif

namespace {

inline void graderFail(const char* expr, const char* file, int line) noexcept
{
    std::fprintf(stderr, "GRADER_CHECK failed at %s:%d: %s\n", file, line, expr);
    std::exit(1);
}

#define GRADER_CHECK(cond)                                    \
    do {                                                      \
        if (!(cond)) {                                        \
            graderFail(#cond, __FILE__, __LINE__);            \
        }                                                     \
    } while (false)

}  // namespace

int main()
{
    const DecodeResult nullResult = decodeSupplyVoltage(nullptr, 2U);
    GRADER_CHECK(nullResult.status == DecodeStatus::NullInput);
    GRADER_CHECK(nullResult.millivolts == 0);

    const std::array<std::uint8_t, 1U> shortBytes{0x01U};
    const DecodeResult emptyResult = decodeSupplyVoltage(shortBytes.data(), 0U);
    GRADER_CHECK(emptyResult.status == DecodeStatus::InvalidLength);
    GRADER_CHECK(emptyResult.millivolts == 0);

    const DecodeResult oneByteResult = decodeSupplyVoltage(shortBytes.data(), 1U);
    GRADER_CHECK(oneByteResult.status == DecodeStatus::InvalidLength);
    GRADER_CHECK(oneByteResult.millivolts == 0);

    const std::array<std::uint8_t, 2U> zeroBytes{0x00U, 0x00U};
    const DecodeResult zeroResult = decodeSupplyVoltage(zeroBytes.data(), zeroBytes.size());
    GRADER_CHECK(zeroResult.status == DecodeStatus::Ok);
    GRADER_CHECK(zeroResult.millivolts == 0);

    // 0x03E8 = 1000 counts -> 5000 mV
    const std::array<std::uint8_t, 2U> midBytes{0x03U, 0xE8U};
    const DecodeResult midResult = decodeSupplyVoltage(midBytes.data(), midBytes.size());
    GRADER_CHECK(midResult.status == DecodeStatus::Ok);
    GRADER_CHECK(midResult.millivolts == 5000);

    // 0x0FFF = 4095 counts -> 20475 mV
    const std::array<std::uint8_t, 2U> maxBytes{0x0FU, 0xFFU};
    const DecodeResult maxResult = decodeSupplyVoltage(maxBytes.data(), maxBytes.size());
    GRADER_CHECK(maxResult.status == DecodeStatus::Ok);
    GRADER_CHECK(maxResult.millivolts == 20475);

    // 0x1000 = 4096 counts -> OutOfRange
    const std::array<std::uint8_t, 2U> overBytes{0x10U, 0x00U};
    const DecodeResult overResult = decodeSupplyVoltage(overBytes.data(), overBytes.size());
    GRADER_CHECK(overResult.status == DecodeStatus::OutOfRange);
    GRADER_CHECK(overResult.millivolts == 0);

    return 0;
}
