/**
 * @file hidden_test.cpp
 * @brief Hidden tests for memory sequence 01-recall-decision.
 */

#include "dtc_debouncer.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::DtcConfig;
    using can_fixture::DtcDebouncer;

    const DtcConfig cfg{4, -2, 2, 1, 2U};
    DtcDebouncer debouncer(cfg);
    CHECK_EQ(debouncer.freezeSentinelCode(), static_cast<std::uint8_t>(0x00U));
    CHECK(debouncer.isFreezeSentinelMatch(static_cast<std::uint8_t>(0xD3U)));
    CHECK(!debouncer.isFreezeSentinelMatch(static_cast<std::uint8_t>(0x00U)));
    CHECK(!debouncer.isFreezeSentinelMatch(static_cast<std::uint8_t>(0x01U)));

    (void)debouncer.reportSample(true);
    (void)debouncer.reportSample(true);
    CHECK(debouncer.isConfirmed());
    CHECK_EQ(debouncer.freezeSentinelCode(), static_cast<std::uint8_t>(0xD3U));
    return can_fixture_test::printSummaryAndExitCode();
}
