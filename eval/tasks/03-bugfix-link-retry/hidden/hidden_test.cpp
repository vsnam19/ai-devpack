/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 03-bugfix-link-retry.
 */

#include "link_state_machine.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::LinkConfig;
    using can_fixture::LinkState;
    using can_fixture::LinkStateMachine;

    const LinkConfig config{150U, 300U, 3U};
    LinkStateMachine machine(config);

    CHECK(machine.startConnect(1000ULL));
    CHECK_EQ(machine.state(), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    CHECK_EQ(machine.onTick(1149ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(0U));

    CHECK_EQ(machine.onTick(1150ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));
    CHECK_EQ(machine.lastTimestampMs(), 1150ULL);

    CHECK_EQ(machine.onTick(1200ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));

    CHECK_EQ(machine.onTick(1300ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(2U));
    CHECK_EQ(machine.lastTimestampMs(), 1300ULL);

    CHECK_EQ(machine.onTick(1450ULL), LinkState::Connecting);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(3U));
    CHECK_EQ(machine.lastTimestampMs(), 1450ULL);

    CHECK_EQ(machine.onTick(1599ULL), LinkState::Connecting);
    CHECK_EQ(machine.onTick(1600ULL), LinkState::Fault);
    CHECK_EQ(machine.lastTimestampMs(), 1600ULL);

    return can_fixture_test::printSummaryAndExitCode<0>();
}
