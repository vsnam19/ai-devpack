/** @file signal.hpp @brief Signal update interface. */
#pragma once
#include <cstdint>
/**
 * @brief Update a signal frame from a supplied value.
 * @param[in,out] frame Frame updated with the supplied value.
 * @param[in] value Value to store in the frame.
 * @return Zero when the frame was updated.
 */
std::uint16_t updateSignal(std::uint16_t& frame, std::uint16_t value);
