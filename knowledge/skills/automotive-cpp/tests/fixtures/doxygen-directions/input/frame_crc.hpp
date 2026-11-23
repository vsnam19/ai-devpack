#ifndef FRAME_CRC_HPP
#define FRAME_CRC_HPP

#include <cstddef>
#include <cstdint>

/**
 * @brief Update a running CRC-16 accumulator over a CAN frame payload.
 * @param[in]  crcAccumulator CRC-16 accumulator updated in place.
 * @param[out] buf            Pointer to payload bytes.
 * @param[out] length         Number of payload bytes.
 * @param      isValidOut     Set true when payload pointer and length are valid.
 */
bool updateFrameCrc(
    std::uint16_t& crcAccumulator,
    const std::uint8_t* buf,
    std::size_t length,
    bool& isValidOut);

#endif
