/**
 * @file hidden_test.cpp
 * @brief Hidden grading test for task 09-doxygen-naming-link.
 */

#include "link_state_machine.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::LinkConfig;
    using can_fixture::LinkState;
    using can_fixture::LinkStateMachine;

    const LinkConfig config{100U, 200U, 2U};
    LinkStateMachine machine(config);
    CHECK(machine.isConfigValid());
    CHECK(machine.startConnect(10ULL));
    CHECK_EQ(machine.state(), LinkState::Connecting);
    CHECK(machine.onHandshakeAck(50ULL));
    CHECK_EQ(machine.state(), LinkState::Active);
    CHECK_EQ(machine.onTick(250ULL), LinkState::Degraded);
    CHECK_EQ(machine.retryCount(), static_cast<std::uint8_t>(1U));
    machine.reset();
    CHECK_EQ(machine.state(), LinkState::Idle);

    return can_fixture_test::printSummaryAndExitCode<0>();
}
