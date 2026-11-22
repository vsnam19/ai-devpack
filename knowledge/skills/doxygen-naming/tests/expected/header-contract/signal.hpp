/**
 * @file signal.hpp
 * @brief Signal scaling interface.
 */
#pragma once
#include <cstdint>
/**
 * @brief Scale a raw signal into engineering units.
 * @param[in] rawValue Raw signal value.
 * @param[out] scaledValue Scaled signal value.
 * @return Zero when scaling succeeds.
 */
std::uint16_t scaleValue(std::uint16_t rawValue, std::uint16_t& scaledValue);
