/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 12-vague-dtc-healing (REQ-DTC-003).
 */

#include "dtc_debouncer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::DtcConfig;
    using can_fixture::DtcDebouncer;
    using can_fixture::DtcState;

    // Boundary 1: single-cycle aging (agingThreshold == 1U)
    const DtcConfig singleCycleConfig{4, -2, 2, 2, 1U};
    DtcDebouncer singleCycle(singleCycleConfig);
    CHECK_EQ(singleCycle.reportSample(true), DtcState::Pending);
    CHECK_EQ(singleCycle.reportSample(true), DtcState::Confirmed);
    CHECK(!singleCycle.onOperationCycleEnd());

    CHECK_EQ(singleCycle.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(singleCycle.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(singleCycle.counter(), static_cast<std::int16_t>(0));
    CHECK(!singleCycle.onOperationCycleEnd());

    CHECK_EQ(singleCycle.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(singleCycle.counter(), static_cast<std::int16_t>(-2));
    CHECK(singleCycle.onOperationCycleEnd());
    CHECK_EQ(singleCycle.state(), DtcState::Inactive);
    CHECK(!singleCycle.isConfirmed());
    CHECK_EQ(singleCycle.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK_EQ(singleCycle.counter(), static_cast<std::int16_t>(0));

    // Subsequent fault sample must count from 0, not from clearThreshold (-2)
    CHECK_EQ(singleCycle.reportSample(true), DtcState::Pending);
    CHECK_EQ(singleCycle.counter(), static_cast<std::int16_t>(2));

    // Boundary 2: two-cycle aging with fault recurrence during Healing
    const DtcConfig twoCycleConfig{6, -4, 2, 2, 2U};
    DtcDebouncer twoCycle(twoCycleConfig);
    CHECK_EQ(twoCycle.reportSample(true), DtcState::Pending);
    CHECK_EQ(twoCycle.reportSample(true), DtcState::Pending);
    CHECK_EQ(twoCycle.reportSample(true), DtcState::Confirmed);

    for (int i = 0; i < 5; ++i) {
        CHECK_EQ(twoCycle.reportSample(false), DtcState::Confirmed);
    }
    CHECK_EQ(twoCycle.counter(), static_cast<std::int16_t>(-4));

    CHECK(twoCycle.onOperationCycleEnd());
    CHECK_EQ(twoCycle.state(), DtcState::Healing);
    CHECK_EQ(twoCycle.healingCounter(), static_cast<std::uint8_t>(1U));
    CHECK(twoCycle.isConfirmed());

    // Fault recurrence during Healing resets healingCounter and restores Confirmed
    CHECK_EQ(twoCycle.reportSample(true), DtcState::Confirmed);
    CHECK_EQ(twoCycle.counter(), static_cast<std::int16_t>(-2));
    CHECK_EQ(twoCycle.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK(!twoCycle.onOperationCycleEnd());

    CHECK_EQ(twoCycle.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(twoCycle.counter(), static_cast<std::int16_t>(-4));
    CHECK(twoCycle.onOperationCycleEnd());
    CHECK_EQ(twoCycle.state(), DtcState::Healing);
    CHECK_EQ(twoCycle.healingCounter(), static_cast<std::uint8_t>(1U));
    CHECK(twoCycle.onOperationCycleEnd());
    CHECK_EQ(twoCycle.state(), DtcState::Inactive);
    CHECK(!twoCycle.isConfirmed());

    // Invalid config rejects operation cycle end
    DtcDebouncer invalidDebouncer(DtcConfig{6, -4, 2, 2, 0U});
    CHECK(!invalidDebouncer.onOperationCycleEnd());

    return can_fixture_test::printSummaryAndExitCode<0>();
}
