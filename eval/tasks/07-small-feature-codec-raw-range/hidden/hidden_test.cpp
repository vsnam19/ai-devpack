/**
 * @file hidden_test.cpp
 * @brief Hidden grading tests for task 07-small-feature-codec-raw-range.
 */

#include <limits>

#include "can_codec.hpp"
#include "test_framework.hpp"

int main() {
    using can_fixture::Endianness;
    using can_fixture::SignalDef;

    const SignalDef invalidSig{60U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    CHECK(!can_fixture::isRawInRange(invalidSig, 0));

    const SignalDef unsigned8{0U, 8U, Endianness::Little, false, 1.0, 0.0, 0.0, 255.0};
    CHECK(!can_fixture::isRawInRange(unsigned8, -1));
    CHECK(can_fixture::isRawInRange(unsigned8, 0));
    CHECK(can_fixture::isRawInRange(unsigned8, 255));
    CHECK(!can_fixture::isRawInRange(unsigned8, 256));

    const SignalDef unsigned1{0U, 1U, Endianness::Little, false, 1.0, 0.0, 0.0, 1.0};
    CHECK(!can_fixture::isRawInRange(unsigned1, -1));
    CHECK(can_fixture::isRawInRange(unsigned1, 0));
    CHECK(can_fixture::isRawInRange(unsigned1, 1));
    CHECK(!can_fixture::isRawInRange(unsigned1, 2));

    const SignalDef unsigned64{0U, 64U, Endianness::Little, false, 1.0, 0.0, 0.0, 100.0};
    CHECK(!can_fixture::isRawInRange(unsigned64, -1));
    CHECK(can_fixture::isRawInRange(unsigned64, std::numeric_limits<std::int64_t>::max()));

    const SignalDef signed1{0U, 1U, Endianness::Little, true, 1.0, 0.0, -1.0, 0.0};
    CHECK(!can_fixture::isRawInRange(signed1, -2));
    CHECK(can_fixture::isRawInRange(signed1, -1));
    CHECK(can_fixture::isRawInRange(signed1, 0));
    CHECK(!can_fixture::isRawInRange(signed1, 1));

    const SignalDef signed8{0U, 8U, Endianness::Little, true, 1.0, 0.0, -128.0, 127.0};
    CHECK(!can_fixture::isRawInRange(signed8, -129));
    CHECK(can_fixture::isRawInRange(signed8, -128));
    CHECK(can_fixture::isRawInRange(signed8, 127));
    CHECK(!can_fixture::isRawInRange(signed8, 128));

    const SignalDef signed64{0U, 64U, Endianness::Big, true, 1.0, 0.0, -100.0, 100.0};
    CHECK(can_fixture::isRawInRange(signed64, std::numeric_limits<std::int64_t>::min()));
    CHECK(can_fixture::isRawInRange(signed64, std::numeric_limits<std::int64_t>::max()));

    return can_fixture_test::printSummaryAndExitCode<0>();
}
