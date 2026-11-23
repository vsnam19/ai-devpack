/**
 * @file grader_test.cpp
 * @brief Known-answer grader for the doxygen-directions behaviour task.
 *
 * Expected CRC values were produced by running the original (pre-task) fixture
 * algorithm: CRC-16 with polynomial 0x1021, no reflection, no final XOR, and the
 * accumulator used as the initial value. The 9-byte check value 0x29B1 matches the
 * standard CRC-16/CCITT-FALSE check value for "123456789".
 */

#include "frame_crc.hpp"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

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

/**
 * @brief Compute the CRC of a NUL-free ASCII string starting from 0xFFFF.
 * @param[in] text Null-terminated text to hash.
 * @return CRC value reported by updateFrameCrc, or 0xFFFF if the update was rejected.
 */
std::uint16_t crcOfText(const char* text) noexcept
{
    std::uint16_t crc = 0xFFFFU;
    bool valid = false;
    const bool updated = updateFrameCrc(crc, reinterpret_cast<const std::uint8_t*>(text),
                                        std::strlen(text), valid);
    GRADER_CHECK(updated);
    GRADER_CHECK(valid);
    return crc;
}

}  // namespace

int main()
{
    std::uint16_t crc = 0xFFFFU;
    bool valid = true;

    GRADER_CHECK(!updateFrameCrc(crc, nullptr, 4U, valid));
    GRADER_CHECK(!valid);
    GRADER_CHECK(crc == 0xFFFFU);

    const std::array<std::uint8_t, 4U> payload{0x01U, 0x02U, 0x03U, 0x04U};
    GRADER_CHECK(!updateFrameCrc(crc, payload.data(), 0U, valid));
    GRADER_CHECK(!valid);

    GRADER_CHECK(!updateFrameCrc(crc, payload.data(), 65U, valid));
    GRADER_CHECK(!valid);

    GRADER_CHECK(updateFrameCrc(crc, payload.data(), payload.size(), valid));
    GRADER_CHECK(valid);
    GRADER_CHECK(crc == 0x89C3U);

    const std::uint16_t firstCrc = crc;
    std::uint16_t repeatCrc = 0xFFFFU;
    GRADER_CHECK(updateFrameCrc(repeatCrc, payload.data(), payload.size(), valid));
    GRADER_CHECK(valid);
    GRADER_CHECK(repeatCrc == firstCrc);

    // Known-answer vectors from the original fixture algorithm.
    GRADER_CHECK(crcOfText("123456789") == 0x29B1U);
    GRADER_CHECK(crcOfText("CAN") == 0x1F96U);

    const std::vector<std::uint8_t> maxFrame(64U, 0xA5U);
    std::uint16_t maxCrc = 0xFFFFU;
    GRADER_CHECK(updateFrameCrc(maxCrc, maxFrame.data(), maxFrame.size(), valid));
    GRADER_CHECK(valid);
    GRADER_CHECK(maxCrc == 0x0E6EU);

    // Successive update: CRC of "1234" then "56789" through the accumulator equals CRC of "123456789".
    std::uint16_t chainedCrc = 0xFFFFU;
    GRADER_CHECK(updateFrameCrc(chainedCrc, reinterpret_cast<const std::uint8_t*>("1234"), 4U, valid));
    GRADER_CHECK(valid);
    GRADER_CHECK(updateFrameCrc(chainedCrc, reinterpret_cast<const std::uint8_t*>("56789"), 5U, valid));
    GRADER_CHECK(valid);
    GRADER_CHECK(chainedCrc == crcOfText("123456789"));
    return 0;
}
