/**
 * @file sensor_hub.hpp
 * @brief Sensor aggregation interface.
 */
#pragma once

#include <cstdint>

namespace org {

/** @brief Aggregate periodic sensor readings. */
class SensorHub {
public:
    /**
     * @brief Sample active sensors.
     * @param[in] mask Channel bitmask.
     * @return Active sample count.
     */
    std::uint32_t sampleAll(std::uint32_t mask) const noexcept;
};

}  // namespace org
