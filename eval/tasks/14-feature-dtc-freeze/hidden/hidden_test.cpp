/**
 * @file hidden_test.cpp
 * @brief Hidden boundary tests for task 14-feature-dtc-freeze (REQ-DTC-005).
 */

#include "dtc_debouncer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::DtcConfig;
    using can_fixture::DtcDebouncer;
    using can_fixture::DtcState;

    const DtcConfig config{6, -4, 3, 2, 2U};
    DtcDebouncer debouncer(config);

    // 1. Initial state is unfrozen
    CHECK(!debouncer.isFrozen());

    // 2. Freezing while Inactive blocks sample accumulation
    debouncer.setFrozen(true);
    CHECK(debouncer.isFrozen());
    CHECK_EQ(debouncer.reportSample(true), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(0));
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK(!debouncer.isConfirmed());
    CHECK(!debouncer.onOperationCycleEnd());

    // 3. Unfreezing resumes normal confirmation
    debouncer.setFrozen(false);
    CHECK(!debouncer.isFrozen());
    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(3));
    CHECK_EQ(debouncer.reportSample(true), DtcState::Confirmed);
    CHECK(debouncer.isConfirmed());

    // 4. Drive counter down to clearThreshold, then freeze before operation cycle end
    for (int i = 0; i < 5; ++i) {
        CHECK_EQ(debouncer.reportSample(false), DtcState::Confirmed);
    }
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-4));

    debouncer.setFrozen(true);
    CHECK(!debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Confirmed);
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK_EQ(debouncer.reportSample(true), DtcState::Confirmed);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-4));

    // 5. Unfreeze, enter Healing, then freeze during Healing
    debouncer.setFrozen(false);
    CHECK(debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Healing);
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(1U));

    debouncer.setFrozen(true);
    CHECK_EQ(debouncer.reportSample(true), DtcState::Healing);
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(1U));
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-4));
    CHECK(!debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Healing);
    CHECK(debouncer.isConfirmed());

    // 6. reset() clears m_isFrozen along with counters and latch
    debouncer.reset();
    CHECK(!debouncer.isFrozen());
    CHECK_EQ(debouncer.state(), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(0));
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK(!debouncer.isConfirmed());

    // Verify monitor works immediately after reset without calling setFrozen(false)
    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(3));

    return can_fixture_test::printSummaryAndExitCode<0>();
}
