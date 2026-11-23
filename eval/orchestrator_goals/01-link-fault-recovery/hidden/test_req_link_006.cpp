/**
 * @file test_req_link_006.cpp
 * @brief Hidden verification tests for orchestrator goal 01-link-fault-recovery (REQ-LINK-006).
 */

#include <cstdint>
#include <iostream>

#include "link_state_machine.hpp"

namespace {

int g_checks = 0;
int g_failures = 0;

void checkCond(bool cond, const char* msg) {
    ++g_checks;
    if (!cond) {
        std::cerr << "FAIL: " << msg << "\n";
        ++g_failures;
    }
}

}  // namespace

int main() {
    using can_fixture::LinkConfig;
    using can_fixture::LinkState;
    using can_fixture::LinkStateMachine;

    const LinkConfig cfg{100U, 200U, 1U};
    LinkStateMachine machine(cfg);
    checkCond(machine.faultCount() == 0U, "initial faultCount must be 0");
    checkCond(!machine.recoverFromFault(10ULL), "recoverFromFault must fail in Idle");

    // Fault via handshake timeout exhaustion
    checkCond(machine.startConnect(100ULL), "startConnect succeeds");
    checkCond(machine.onTick(200ULL) == LinkState::Connecting, "retry 1");
    checkCond(machine.onTick(300ULL) == LinkState::Fault, "fault on handshake exhaustion");
    checkCond(machine.faultCount() == 1U, "faultCount incremented to 1");

    // Non-monotonic timestamp rejected
    checkCond(!machine.recoverFromFault(299ULL), "reject non-monotonic timestamp");
    checkCond(machine.state() == LinkState::Fault, "remain in Fault after rejected recovery");

    // Valid recovery transitions to Connecting and preserves faultCount
    checkCond(machine.recoverFromFault(350ULL), "recoverFromFault succeeds");
    checkCond(machine.state() == LinkState::Connecting, "state becomes Connecting");
    checkCond(machine.retryCount() == 0U, "retryCount reset to 0");
    checkCond(machine.lastTimestampMs() == 350ULL, "lastTimestampMs updated");
    checkCond(machine.faultCount() == 1U, "faultCount preserved across recovery");

    // Connect, go Active, and fault via heartbeat exhaustion
    checkCond(machine.onHandshakeAck(360ULL), "ack succeeds");
    checkCond(machine.onTick(560ULL) == LinkState::Degraded, "enters Degraded");
    checkCond(machine.onTick(760ULL) == LinkState::Fault, "fault on heartbeat exhaustion");
    checkCond(machine.faultCount() == 2U, "faultCount incremented to 2");

    // Reset clears faultCount
    machine.reset();
    checkCond(machine.state() == LinkState::Idle, "reset returns Idle");
    checkCond(machine.faultCount() == 0U, "reset clears faultCount");

    std::cout << "DEVPACK_TESTS_COMPLETED checks=" << g_checks
              << " failed=" << g_failures << "\n"
              << std::flush;
    if (g_checks == 0 || g_failures != 0) {
        return 1;
    }
    return 0;
}
