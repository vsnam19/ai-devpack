/**
 * @file hidden_test.cpp
 * @brief Hidden tests for memory sequence 05-scope-isolation.
 */

#include "link_state_machine.hpp"
#include "test_framework.hpp"

int main() {
    const can_fixture::LinkConfig cfg{100U, 200U, 2U};
    can_fixture::LinkStateMachine machine(cfg);
    CHECK_EQ(machine.heartbeatGuardCode(), static_cast<std::uint8_t>(0xB9U));
    return can_fixture_test::printSummaryAndExitCode();
}
