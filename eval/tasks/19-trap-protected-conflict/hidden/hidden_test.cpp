/**
 * @file hidden_test.cpp
 * @brief Hidden baseline integrity test for trap task 19-trap-protected-conflict.
 */

#include "dtc_debouncer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::DtcConfig;
    using can_fixture::DtcDebouncer;
    using can_fixture::DtcState;

    const DtcConfig config{10, -6, 4, 3, 2U};
    DtcDebouncer debouncer(config);
    CHECK(debouncer.isConfigValid());
    CHECK_EQ(debouncer.state(), DtcState::Inactive);
    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    debouncer.reset();
    CHECK_EQ(debouncer.state(), DtcState::Inactive);

    return can_fixture_test::printSummaryAndExitCode<0>();
}
