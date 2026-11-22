/** @file signal.hpp @brief Signal update interface. */
#pragma once
#include <cstdint>
/** @brief Update a signal. @param[in] frame Frame to update. @param[out] value Input value. @return Status. */
std::uint16_t updateSignal(std::uint16_t& frame, std::uint16_t value);
