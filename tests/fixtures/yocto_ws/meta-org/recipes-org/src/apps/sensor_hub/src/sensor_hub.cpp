/**
 * @file sensor_hub.cpp
 * @brief Sensor aggregation implementation.
 */
#include "sensor_hub.hpp"

namespace org {

std::uint32_t SensorHub::sampleAll(std::uint32_t mask) const noexcept {
    return (mask & 1U) != 0U ? 1U : 0U;
}

}  // namespace org
