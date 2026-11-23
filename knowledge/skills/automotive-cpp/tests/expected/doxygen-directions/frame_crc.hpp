/**
 * @file frame_crc.hpp
 * @brief Bounded CRC-16 frame accumulator interface.
 */

#ifndef FRAME_CRC_HPP
#define FRAME_CRC_HPP

#include <cstddef>
#include <cstdint>

/**
 * @brief Update a running CRC-16 accumulator over a bounded frame payload.
 * @param[in,out] crcAccumulator CRC-16 accumulator updated with payload bytes.
 * @param[in]     frameBytes     Pointer to input payload byte array.
 * @param[in]     byteCount      Number of payload bytes to process (1..64).
 * @param[out]    isValidOut     Set true when inputs are valid and CRC was updated.
 * @return True if inputs were valid and CRC was updated, false otherwise.
 */
bool updateFrameCrc(
    std::uint16_t& crcAccumulator,
    const std::uint8_t* frameBytes,
    std::size_t byteCount,
    bool& isValidOut) noexcept;

#endif
