/**
 * @file test_main.cpp
 * @brief Entry point executing all evaluation fixture unit test suites.
 */

#include "test_framework.hpp"

/**
 * @brief Execute all fixture unit tests and return 0 if all checks pass.
 * @return Process exit code (0 on success, 1 on any test failure).
 */
int main() {
    can_fixture_test::runCanCodecTests();
    can_fixture_test::runRingBufferTests();
    can_fixture_test::runLinkStateMachineTests();
    can_fixture_test::runDtcDebouncerTests();
    return can_fixture_test::printSummaryAndExitCode<0>();
}
