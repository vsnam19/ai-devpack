#include "frame_crc.hpp"

#include <array>
#include <cassert>
#include <cstdint>

int main()
{
    std::uint16_t crc = 0xFFFFU;
    bool valid = true;

    assert(!updateFrameCrc(crc, nullptr, 4U, valid));
    assert(!valid);
    assert(crc == 0xFFFFU);

    const std::array<std::uint8_t, 4U> payload{0x01U, 0x02U, 0x03U, 0x04U};
    assert(!updateFrameCrc(crc, payload.data(), 0U, valid));
    assert(!valid);

    assert(!updateFrameCrc(crc, payload.data(), 65U, valid));
    assert(!valid);

    assert(updateFrameCrc(crc, payload.data(), payload.size(), valid));
    assert(valid);
    assert(crc != 0xFFFFU);

    const std::uint16_t firstCrc = crc;
    std::uint16_t repeatCrc = 0xFFFFU;
    assert(updateFrameCrc(repeatCrc, payload.data(), payload.size(), valid));
    assert(valid);
    assert(repeatCrc == firstCrc);
    return 0;
}
