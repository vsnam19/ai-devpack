/**
 * @file link_state_machine.cpp
 * @brief Implementation of the LinkStateMachine supervision logic.
 */

#include "link_state_machine.hpp"

namespace can_fixture {

LinkStateMachine::LinkStateMachine(const LinkConfig& config) noexcept
    : m_config(config),
      m_state(LinkState::Idle),
      m_lastTimestampMs(0ULL),
      m_retryCount(0U) {
}

bool LinkStateMachine::isConfigValid() const noexcept {
    return (m_config.handshakeTimeoutMs > 0U) &&
           (m_config.heartbeatTimeoutMs > 0U) &&
           (m_config.maxRetries > 0U);
}

bool LinkStateMachine::startConnect(std::uint64_t nowMs) noexcept {
    if (!isConfigValid() || m_state != LinkState::Idle) {
        return false;
    }
    m_state = LinkState::Connecting;
    m_lastTimestampMs = nowMs;
    m_retryCount = 0U;
    return true;
}

bool LinkStateMachine::onHandshakeAck(std::uint64_t nowMs) noexcept {
    if (m_state != LinkState::Connecting || nowMs < m_lastTimestampMs) {
        return false;
    }
    m_state = LinkState::Active;
    m_lastTimestampMs = nowMs;
    m_retryCount = 0U;
    return true;
}

bool LinkStateMachine::onHeartbeat(std::uint64_t nowMs) noexcept {
    if ((m_state != LinkState::Active && m_state != LinkState::Degraded) ||
        nowMs < m_lastTimestampMs) {
        return false;
    }
    m_state = LinkState::Active;
    m_lastTimestampMs = nowMs;
    m_retryCount = 0U;
    return true;
}

LinkState LinkStateMachine::onTick(std::uint64_t nowMs) noexcept {
    if (nowMs <= m_lastTimestampMs) {
        return m_state;
    }
    const std::uint64_t elapsedMs = nowMs - m_lastTimestampMs;

    switch (m_state) {
        case LinkState::Connecting: {
            if (elapsedMs >= static_cast<std::uint64_t>(m_config.handshakeTimeoutMs)) {
                if (m_retryCount < m_config.maxRetries) {
                    ++m_retryCount;
                    m_lastTimestampMs = nowMs;
                } else {
                    m_state = LinkState::Fault;
                    m_lastTimestampMs = nowMs;
                }
            }
            break;
        }
        case LinkState::Active: {
            if (elapsedMs >= static_cast<std::uint64_t>(m_config.heartbeatTimeoutMs)) {
                m_state = LinkState::Degraded;
                m_retryCount = 1U;
                m_lastTimestampMs = nowMs;
            }
            break;
        }
        case LinkState::Degraded: {
            if (elapsedMs >= static_cast<std::uint64_t>(m_config.heartbeatTimeoutMs)) {
                if (m_retryCount < m_config.maxRetries) {
                    ++m_retryCount;
                    m_lastTimestampMs = nowMs;
                } else {
                    m_state = LinkState::Fault;
                    m_lastTimestampMs = nowMs;
                }
            }
            break;
        }
        case LinkState::Idle:
        case LinkState::Fault:
        default: {
            break;
        }
    }
    return m_state;
}

void LinkStateMachine::reset() noexcept {
    m_state = LinkState::Idle;
    m_lastTimestampMs = 0ULL;
    m_retryCount = 0U;
}

LinkState LinkStateMachine::state() const noexcept {
    return m_state;
}

std::uint8_t LinkStateMachine::retryCount() const noexcept {
    return m_retryCount;
}

std::uint64_t LinkStateMachine::lastTimestampMs() const noexcept {
    return m_lastTimestampMs;
}

}  // namespace can_fixture
