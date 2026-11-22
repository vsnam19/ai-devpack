/**
 * @file test_dtc_debouncer.cpp
 * @brief Unit tests for DtcDebouncer configuration validation, fault confirmation, aging, and reset.
 */

#include "dtc_debouncer.hpp"
#include "test_framework.hpp"

namespace can_fixture_test {

namespace {

using can_fixture::DtcConfig;
using can_fixture::DtcDebouncer;
using can_fixture::DtcState;

/**
 * @brief Verify configuration parameter validation and initial state invariants.
 * @req REQ-DTC-001
 */
void testConfigValidationAndInitialState() {
    const DtcConfig validConfig{10, -10, 5, 2, 3U};
    DtcDebouncer debouncer(validConfig);

    CHECK(debouncer.isConfigValid());
    CHECK_EQ(debouncer.state(), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(0));
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK(!debouncer.isConfirmed());

    const DtcConfig zeroConfirmConfig{0, 0, 5, 2, 3U};
    DtcDebouncer invalidConfirm(zeroConfirmConfig);
    CHECK(!invalidConfirm.isConfigValid());
    CHECK_EQ(invalidConfirm.reportSample(true), DtcState::Inactive);

    const DtcConfig positiveClearConfig{10, 1, 5, 2, 3U};
    DtcDebouncer invalidClear(positiveClearConfig);
    CHECK(!invalidClear.isConfigValid());

    const DtcConfig zeroStepConfig{10, 0, 0, 2, 3U};
    DtcDebouncer invalidStep(zeroStepConfig);
    CHECK(!invalidStep.isConfigValid());

    const DtcConfig zeroAgingConfig{10, 0, 5, 2, 0U};
    DtcDebouncer invalidAging(zeroAgingConfig);
    CHECK(!invalidAging.isConfigValid());
}

/**
 * @brief Verify asymmetric fail/pass step counting, Pending state, and Confirmed latching.
 * @req REQ-DTC-002
 */
void testPendingAndConfirmDebounceThresholds() {
    const DtcConfig config{10, -6, 4, 3, 2U};
    DtcDebouncer debouncer(config);

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(4));
    CHECK(!debouncer.isConfirmed());

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(8));

    CHECK_EQ(debouncer.reportSample(false), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(5));

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(9));

    CHECK_EQ(debouncer.reportSample(true), DtcState::Confirmed);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(10));
    CHECK(debouncer.isConfirmed());

    CHECK_EQ(debouncer.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(7));
    CHECK(debouncer.isConfirmed());
}

/**
 * @brief Verify multi-cycle aging/healing from Confirmed to Healing and back to Inactive.
 * @req REQ-DTC-003
 */
void testOperationCycleAgingAndHealing() {
    const DtcConfig config{6, -4, 3, 2, 3U};
    DtcDebouncer debouncer(config);

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.reportSample(true), DtcState::Confirmed);
    CHECK(!debouncer.onOperationCycleEnd());

    for (int i = 0; i < 5; ++i) {
        CHECK_EQ(debouncer.reportSample(false), DtcState::Confirmed);
    }
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-4));

    CHECK(debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Healing);
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(1U));
    CHECK(debouncer.isConfirmed());

    CHECK(debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Healing);
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(2U));

    CHECK(debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Inactive);
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(0));
    CHECK(!debouncer.isConfirmed());
}

/**
 * @brief Verify unconfirmed pass recovery to Inactive and diagnostic reset from Confirmed.
 * @req REQ-DTC-004
 */
void testResetAndUnconfirmedPassRecovery() {
    const DtcConfig config{8, -4, 3, 4, 2U};
    DtcDebouncer debouncer(config);

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(3));

    CHECK_EQ(debouncer.reportSample(false), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-1));

    CHECK_EQ(debouncer.reportSample(false), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-4));

    CHECK_EQ(debouncer.reportSample(true), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-1));

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.reportSample(true), DtcState::Confirmed);
    CHECK(debouncer.isConfirmed());

    debouncer.reset();
    CHECK_EQ(debouncer.state(), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(0));
    CHECK_EQ(debouncer.healingCounter(), static_cast<std::uint8_t>(0U));
    CHECK(!debouncer.isConfirmed());
}

}  // namespace

void runDtcDebouncerTests() {
    testConfigValidationAndInitialState();
    testPendingAndConfirmDebounceThresholds();
    testOperationCycleAgingAndHealing();
    testResetAndUnconfirmedPassRecovery();
}

}  // namespace can_fixture_test
