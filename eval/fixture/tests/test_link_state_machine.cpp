/**
 * @file test_link_state_machine.cpp
 * @brief Unit tests for LinkStateMachine connection setup, retries, heartbeat supervision, and reset.
 */

#include "link_state_machine.hpp"
#include "test_framework.hpp"

namespace can_fixture_test {

namespace {

using can_fixture::LinkConfig;
using can_fixture::LinkState;
using can_fixture::LinkStateMachine;

/**
 * @brief Verify transition from Idle to Connecting on startConnect and to Active on onHandshakeAck.
 * @req REQ-LINK-001
 */
void testHandshakeTransitions() {
    const LinkConfig config{100U, 250U, 3U};
    LinkStateMachine machine(config);

    CHECK(machine.isConfigValid());
    CHECK_EQ(machine.state(), LinkState::Idle);
    CHECK(!machine.onHandshakeAck(10ULL));

    CHECK(machine.startConnect(1000ULL));
    CHECK_EQ(machine.state(), LinkState::Connecting);
    CHECK_EQ(machine.lastTimestampMs(), 1000ULL);
    CHECK(!machine.startConnect(1010ULL));

    CHECK(machine.onHandshakeAck(1050ULL));
    CHECK_EQ(machine.state(), LinkState::Active);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));
    CHECK_EQ(machine.lastTimestampMs(), 1050ULL);
}

/**
 * @brief Verify Connecting retries up to maxRetries on handshakeTimeoutMs before entering Fault.
 * @req REQ-LINK-002
 */
void testHandshakeRetryAndFault() {
    const LinkConfig config{100U, 200U, 2U};
    LinkStateMachine machine(config);

    CHECK(machine.startConnect(0ULL));
    CHECK_EQ(machine.onTick(99ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    CHECK_EQ(machine.onTick(100ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));

    CHECK_EQ(machine.onTick(199ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));

    CHECK_EQ(machine.onTick(200ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(2U));

    CHECK_EQ(machine.onTick(300ULL), LinkState::Fault);
}

/**
 * @brief Verify Active enters Degraded on missed heartbeat, recovers on heartbeat, or faults.
 * @req REQ-LINK-003
 */
void testHeartbeatSupervisionAndDegradedEscalation() {
    const LinkConfig config{100U, 200U, 2U};
    LinkStateMachine machine(config);

    CHECK(machine.startConnect(0ULL));
    CHECK(machine.onHandshakeAck(50ULL));
    CHECK_EQ(machine.state(), LinkState::Active);

    CHECK(machine.onHeartbeat(150ULL));
    CHECK_EQ(machine.state(), LinkState::Active);
    CHECK_EQ(machine.lastTimestampMs(), 150ULL);

    CHECK_EQ(machine.onTick(350ULL), LinkState::Degraded);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));

    CHECK(machine.onHeartbeat(400ULL));
    CHECK_EQ(machine.state(), LinkState::Active);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    CHECK_EQ(machine.onTick(600ULL), LinkState::Degraded);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));
    CHECK_EQ(machine.onTick(800ULL), LinkState::Degraded);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(2U));
    CHECK_EQ(machine.onTick(1000ULL), LinkState::Fault);
}

/**
 * @brief Verify reset returns the state machine from Fault or Active to Idle with zero retryCount.
 * @req REQ-LINK-004
 */
void testResetFromAnyState() {
    const LinkConfig config{50U, 100U, 1U};
    LinkStateMachine machine(config);

    CHECK(machine.startConnect(10ULL));
    CHECK_EQ(machine.onTick(60ULL), LinkState::Connecting);
    CHECK_EQ(machine.onTick(110ULL), LinkState::Fault);

    machine.reset();
    CHECK_EQ(machine.state(), LinkState::Idle);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));
    CHECK_EQ(machine.lastTimestampMs(), 0ULL);

    CHECK(machine.startConnect(500ULL));
    CHECK_EQ(machine.state(), LinkState::Connecting);
}

}  // namespace

void runLinkStateMachineTests() {
    testHandshakeTransitions();
    testHandshakeRetryAndFault();
    testHeartbeatSupervisionAndDegradedEscalation();
    testResetFromAnyState();
}

}  // namespace can_fixture_test
