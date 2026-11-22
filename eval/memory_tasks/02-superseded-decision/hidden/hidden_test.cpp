/**
 * @file hidden_test.cpp
 * @brief Hidden tests for memory sequence 02-superseded-decision.
 */

#include "link_state_machine.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::LinkConfig;
    using can_fixture::LinkStateMachine;

    const LinkConfig cfg{100U, 200U, 2U};
    LinkStateMachine machine(cfg);
    CHECK_EQ(machine.recoveryBackoffTicks(), static_cast<std::uint8_t>(7U));
    return can_fixture_test::printSummaryAndExitCode();
}
