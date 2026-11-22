/**
 * @file link_state_machine.hpp
 * @brief Deterministic communication link state machine with handshake and heartbeat timeouts.
 */

#ifndef CAN_FIXTURE_LINK_STATE_MACHINE_HPP
#define CAN_FIXTURE_LINK_STATE_MACHINE_HPP

#include <cstdint>

namespace can_fixture {

/**
 * @brief Operational state of a supervised communication link.
 */
enum class LinkState : std::uint8_t {
    /** @brief Link is inactive and awaiting startConnect(). */
    Idle = 0U,
    /** @brief Handshake initiated and awaiting onHandshakeAck() within handshakeTimeoutMs. */
    Connecting = 1U,
    /** @brief Link is healthy and receiving heartbeats within heartbeatTimeoutMs. */
    Active = 2U,
    /** @brief Link missed at least one heartbeat and is awaiting recovery or fault threshold. */
    Degraded = 3U,
    /** @brief Link exhausted retry budget and requires reset(). */
    Fault = 4U
};

/**
 * @brief Timeout and retry configuration parameters for LinkStateMachine.
 */
struct LinkConfig {
    /** @brief Maximum duration in milliseconds to wait per handshake attempt (must be > 0). */
    std::uint32_t handshakeTimeoutMs;
    /** @brief Maximum duration in milliseconds between heartbeats (must be > 0). */
    std::uint32_t heartbeatTimeoutMs;
    /** @brief Maximum retry attempts before entering Fault state (must be > 0). */
    std::uint8_t maxRetries;
};

/**
 * @brief State machine managing connection setup, heartbeat supervision, and retry escalation.
 */
class LinkStateMachine {
public:
    /**
     * @brief Construct a link state machine in the Idle state with the given configuration.
     * @param[in] config Timeout and retry thresholds.
     */
    explicit LinkStateMachine(const LinkConfig& config) noexcept;

    /**
     * @brief Check whether the configured timeout and retry thresholds are positive.
     * @return True if handshakeTimeoutMs > 0, heartbeatTimeoutMs > 0, and maxRetries > 0.
     */
    bool isConfigValid() const noexcept;

    /**
     * @brief Initiate connection handshake from the Idle state.
     * @param[in] nowMs Current monotonic timestamp in milliseconds.
     * @return True if transitioned from Idle to Connecting, false if config is invalid or not Idle.
     */
    bool startConnect(std::uint64_t nowMs) noexcept;

    /**
     * @brief Process a handshake acknowledgment while in the Connecting state.
     * @param[in] nowMs Current monotonic timestamp in milliseconds (must be >= lastTimestampMs()).
     * @return True if transitioned from Connecting to Active, false otherwise.
     */
    bool onHandshakeAck(std::uint64_t nowMs) noexcept;

    /**
     * @brief Process a peer heartbeat while in Active or Degraded state.
     * @param[in] nowMs Current monotonic timestamp in milliseconds (must be >= lastTimestampMs()).
     * @return True if processed and state is Active with retryCount reset to 0, false otherwise.
     */
    bool onHeartbeat(std::uint64_t nowMs) noexcept;

    /**
     * @brief Advance time supervision and evaluate handshake or heartbeat timeouts.
     * @param[in] nowMs Current monotonic timestamp in milliseconds.
     * @return Resulting LinkState after evaluating elapsed time.
     */
    LinkState onTick(std::uint64_t nowMs) noexcept;

    /**
     * @brief Reset the state machine back to Idle and clear retry and timestamp state.
     */
    void reset() noexcept;

    /**
     * @brief Return the current link state.
     * @return Current LinkState value.
     */
    LinkState state() const noexcept;

    /**
     * @brief Return the current retry count in Connecting or Degraded state.
     * @return Retry counter value.
     */
    std::uint8_t retryCount() const noexcept;

    /**
     * @brief Return the timestamp of the most recent state transition or retry event.
     * @return Monotonic timestamp in milliseconds.
     */
    std::uint64_t lastTimestampMs() const noexcept;

private:
    LinkConfig m_config;
    LinkState m_state;
    std::uint64_t m_lastTimestampMs;
    std::uint8_t m_retryCount;
};

}  // namespace can_fixture

#endif  // CAN_FIXTURE_LINK_STATE_MACHINE_HPP
