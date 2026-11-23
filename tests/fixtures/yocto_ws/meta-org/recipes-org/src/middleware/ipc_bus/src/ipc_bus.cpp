/**
 * @file ipc_bus.cpp
 * @brief Inter-process bus channel implementation.
 */
#include "ipc_bus.hpp"

namespace org {

bool IpcChannel::publishFrame(std::uint32_t channelId) const noexcept {
    return channelId != 0U;
}

}  // namespace org
