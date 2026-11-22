/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 11-vague-link-recovery (REQ-LINK-005 / REQ-LINK-003).
 */

#include "link_state_machine.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::LinkConfig;
    using can_fixture::LinkState;
    using can_fixture::LinkStateMachine;

    const LinkConfig config{100U, 200U, 1U};
    LinkStateMachine machine(config);

    // Reject heartbeat in Idle
    CHECK(!machine.onHeartbeat(10ULL));
    CHECK_EQ(machine.state(), LinkState::Idle);
    CHECK_EQ(machine.lastTimestampMs(), 0ULL);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    // Reject heartbeat in Connecting
    CHECK(machine.startConnect(100ULL));
    CHECK(!machine.onHeartbeat(120ULL));
    CHECK_EQ(machine.state(), LinkState::Connecting);
    CHECK_EQ(machine.lastTimestampMs(), 100ULL);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    // Exhaust handshake retries -> Fault, then verify heartbeat is rejected in Fault
    CHECK_EQ(machine.onTick(200ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));
    CHECK_EQ(machine.onTick(300ULL), LinkState::Fault);

    CHECK(!machine.onHeartbeat(300ULL));
    CHECK(!machine.onHeartbeat(350ULL));
    CHECK_EQ(machine.state(), LinkState::Fault);
    CHECK_EQ(machine.lastTimestampMs(), 300ULL);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));

    // Reset, reach Active, exhaust heartbeat retries -> Fault, verify heartbeat rejected
    machine.reset();
    CHECK(machine.startConnect(1000ULL));
    CHECK(machine.onHandshakeAck(1050ULL));
    CHECK_EQ(machine.state(), LinkState::Active);

    // Non-monotonic timestamp rejected in Active
    CHECK(!machine.onHeartbeat(1049ULL));
    CHECK_EQ(machine.lastTimestampMs(), 1050ULL);

    // Boundary equal timestamp accepted in Degraded
    CHECK_EQ(machine.onTick(1250ULL), LinkState::Degraded);
    CHECK(!machine.onHeartbeat(1249ULL));
    CHECK_EQ(machine.state(), LinkState::Degraded);
    CHECK(machine.onHeartbeat(1250ULL));
    CHECK_EQ(machine.state(), LinkState::Active);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    // Degraded -> Fault -> reject heartbeat
    CHECK_EQ(machine.onTick(1450ULL), LinkState::Degraded);
    CHECK_EQ(machine.onTick(1650ULL), LinkState::Fault);
    CHECK(!machine.onHeartbeat(1650ULL));
    CHECK(!machine.onHeartbeat(1800ULL));
    CHECK_EQ(machine.state(), LinkState::Fault);

    return can_fixture_test::printSummaryAndExitCode<0>();
}
