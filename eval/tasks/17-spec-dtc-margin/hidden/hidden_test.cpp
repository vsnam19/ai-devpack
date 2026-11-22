/**
 * @file hidden_test.cpp
 * @brief Hidden boundary tests for task 17-spec-dtc-margin (REQ-DTC-006).
 */

#include "dtc_debouncer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::DtcConfig;
    using can_fixture::DtcDebouncer;
    using can_fixture::DtcState;
    using can_fixture::kMaxDebounceThreshold;

    // 1. Invalid configuration returns 0
    DtcDebouncer invalid(DtcConfig{0, 0, 1, 1, 1U});
    CHECK_EQ(invalid.remainingToConfirm(), static_cast<std::int16_t>(0));

    // 2. Initial, pending, and negative counter distances
    DtcDebouncer debouncer(DtcConfig{10, -6, 4, 7, 2U});
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(10));

    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(4));
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(6));

    // Pass sample drops counter below 0 while unconfirmed
    CHECK_EQ(debouncer.reportSample(false), DtcState::Inactive);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-3));
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(13));

    // 3. Confirmed and Healing states always return 0 even when counter < confirmThreshold
    for (int i = 0; i < 4; ++i) {
        debouncer.reportSample(true);
    }
    CHECK_EQ(debouncer.state(), DtcState::Confirmed);
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(0));

    CHECK_EQ(debouncer.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(3));
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(0));

    CHECK_EQ(debouncer.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(debouncer.reportSample(false), DtcState::Confirmed);
    CHECK_EQ(debouncer.counter(), static_cast<std::int16_t>(-6));
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(0));

    CHECK(debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Healing);
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(0));

    // Complete aging to Inactive -> counter resets to 0, margin restored to confirmThreshold
    CHECK(debouncer.onOperationCycleEnd());
    CHECK_EQ(debouncer.state(), DtcState::Inactive);
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(10));

    // 4. Max threshold boundary without int16_t overflow: 16383 - (-16383) = 32766
    DtcDebouncer maxRange(DtcConfig{
        kMaxDebounceThreshold,
        static_cast<std::int16_t>(-kMaxDebounceThreshold),
        1,
        kMaxDebounceThreshold,
        1U});
    CHECK_EQ(maxRange.reportSample(false), DtcState::Inactive);
    CHECK_EQ(maxRange.counter(), static_cast<std::int16_t>(-kMaxDebounceThreshold));
    CHECK_EQ(maxRange.remainingToConfirm(), static_cast<std::int16_t>(32766));

    // 5. reset() restores full margin
    CHECK_EQ(debouncer.reportSample(true), DtcState::Pending);
    debouncer.reset();
    CHECK_EQ(debouncer.remainingToConfirm(), static_cast<std::int16_t>(10));

    return can_fixture_test::printSummaryAndExitCode<0>();
}
