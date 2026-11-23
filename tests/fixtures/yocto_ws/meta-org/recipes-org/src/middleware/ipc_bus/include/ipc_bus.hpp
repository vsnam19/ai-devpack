/**
 * @file ipc_bus.hpp
 * @brief Inter-process bus channel interface.
 */
#pragma once

#include <cstdint>

namespace org {

/** @brief Publish framed messages on the local bus. */
class IpcChannel {
public:
    /**
     * @brief Publish a single payload identifier.
     * @param[in] channelId Target channel identifier.
     * @return True when channelId is non-zero.
     */
    bool publishFrame(std::uint32_t channelId) const noexcept;
};

}  // namespace org
