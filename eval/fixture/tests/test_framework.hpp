/**
 * @file test_framework.hpp
 * @brief Minimal header-only unit test framework with CHECK and CHECK_EQ macros.
 */

#ifndef CAN_FIXTURE_TEST_FRAMEWORK_HPP
#define CAN_FIXTURE_TEST_FRAMEWORK_HPP

#include <cmath>
#include <cstddef>
#include <iostream>

namespace can_fixture_test {

/**
 * @brief Aggregate pass and fail counters across all executed test assertions.
 */
struct TestCounters {
    /** @brief Total number of assertions evaluated. */
    std::size_t totalChecks;
    /** @brief Number of assertions that failed. */
    std::size_t failedChecks;
};

/**
 * @brief Access the process-wide singleton TestCounters instance.
 * @tparam Tag Dummy template parameter to keep function header-only under lint rules.
 * @return Reference to global TestCounters.
 */
template <int Tag = 0>
inline TestCounters& counters() noexcept {
    static TestCounters instance{0U, 0U};
    return instance;
}

/**
 * @brief Record a single boolean check result and print diagnostics on failure.
 * @tparam Tag Dummy template parameter for header-only template definition.
 * @param[in] condition Evaluated boolean condition.
 * @param[in] expressionText Source string of the checked expression.
 * @param[in] fileName Source file path of the check.
 * @param[in] lineNumber 1-based source line number of the check.
 */
template <int Tag = 0>
inline void recordCheck(
    bool condition,
    const char* expressionText,
    const char* fileName,
    int lineNumber) {
    TestCounters& state = counters<Tag>();
    ++state.totalChecks;
    if (!condition) {
        ++state.failedChecks;
        std::cerr << "FAIL: " << fileName << ":" << lineNumber
                  << ": CHECK(" << expressionText << ") failed\n";
    }
}

/**
 * @brief Record an equality check between two values and print diagnostics on mismatch.
 * @tparam ActualType Type of the actual expression.
 * @tparam ExpectedType Type of the expected expression.
 * @param[in] actualValue Evaluated actual value.
 * @param[in] expectedValue Evaluated expected value.
 * @param[in] expressionText Combined source expression text for the equality check.
 * @param[in] fileName Source file path of the check.
 * @param[in] lineNumber 1-based source line number of the check.
 */
template <typename ActualType, typename ExpectedType>
inline void recordCheckEq(
    const ActualType& actualValue,
    const ExpectedType& expectedValue,
    const char* expressionText,
    const char* fileName,
    int lineNumber) {
    TestCounters& state = counters<0>();
    ++state.totalChecks;
    if (!(actualValue == expectedValue)) {
        ++state.failedChecks;
        std::cerr << "FAIL: " << fileName << ":" << lineNumber
                  << ": CHECK_EQ(" << expressionText << ") failed\n";
    }
}

/**
 * @brief Print final test summary and return process exit code (0 on pass, 1 on failure).
 * @tparam Tag Dummy template parameter for header-only template definition.
 * @return 0 if at least one check ran and zero checks failed; 1 otherwise.
 */
template <int Tag = 0>
inline int printSummaryAndExitCode() {
    const TestCounters& state = counters<Tag>();
    const std::size_t passedChecks = state.totalChecks - state.failedChecks;
    std::cout << "TEST SUMMARY: " << passedChecks << "/" << state.totalChecks
              << " checks passed (" << state.failedChecks << " failed)\n";
    if (state.totalChecks == 0U || state.failedChecks > 0U) {
        return 1;
    }
    return 0;
}

/**
 * @brief Run all CAN codec unit test suites.
 */
void runCanCodecTests();

/**
 * @brief Run all ring buffer unit test suites.
 */
void runRingBufferTests();

/**
 * @brief Run all link state machine unit test suites.
 */
void runLinkStateMachineTests();

/**
 * @brief Run all DTC debouncer unit test suites.
 */
void runDtcDebouncerTests();

}  // namespace can_fixture_test

#define CHECK(expr) \
    ::can_fixture_test::recordCheck<0>(static_cast<bool>(expr), #expr, __FILE__, __LINE__)

#define CHECK_EQ(actual, expected) \
    ::can_fixture_test::recordCheckEq((actual), (expected), #actual ", " #expected, __FILE__, __LINE__)

#define CHECK_NEAR(actual, expected, tolerance) \
    ::can_fixture_test::recordCheck<0>( \
        std::fabs(static_cast<double>(actual) - static_cast<double>(expected)) <= \
            static_cast<double>(tolerance), \
        #actual " ~= " #expected, \
        __FILE__, \
        __LINE__)

#endif  // CAN_FIXTURE_TEST_FRAMEWORK_HPP
