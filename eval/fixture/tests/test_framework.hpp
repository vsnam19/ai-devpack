/**
 * @file test_framework.hpp
 * @brief Minimal header-only unit test framework with CHECK and CHECK_EQ macros.
 */

#ifndef CAN_FIXTURE_TEST_FRAMEWORK_HPP
#ifdef DEVPACK_FRAMEWORK_GUARD
#define CAN_FIXTURE_TEST_FRAMEWORK_HPP DEVPACK_FRAMEWORK_GUARD
#else
#define CAN_FIXTURE_TEST_FRAMEWORK_HPP 1
#endif

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
    /** @brief Number of assertions that passed. */
    std::size_t passedChecks;
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
    static TestCounters instance{0U, 0U, 0U};
    return instance;
}

/**
 * @brief Record a single failed assertion on a dedicated failure-path line for gcov verification.
 * @tparam Tag Dummy template parameter for header-only template definition.
 * @param[in] checkName Macro name that triggered the failure.
 * @param[in] expressionText Source string of the checked expression.
 * @param[in] fileName Source file path of the check.
 * @param[in] lineNumber 1-based source line number of the check.
 */
template <int Tag = 0>
inline void recordCheckFailure(
    const char* checkName,
    const char* expressionText,
    const char* fileName,
    int lineNumber) {
    TestCounters& state = counters<Tag>();
    ++state.failedChecks;  // DEVPACK_FAILURE_PATH
    std::cerr << "FAIL: " << fileName << ":" << lineNumber
              << ": " << checkName << "(" << expressionText << ") failed\n";
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
    ++state.totalChecks;  // DEVPACK_CHECK_TOTAL
    if (!condition) {
        recordCheckFailure<Tag>("CHECK", expressionText, fileName, lineNumber);  // DEVPACK_FAILURE_PATH
    } else {
        ++state.passedChecks;  // DEVPACK_SUCCESS_PATH
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
    ++state.totalChecks;  // DEVPACK_CHECK_TOTAL
    if (!(actualValue == expectedValue)) {
        recordCheckFailure<0>("CHECK_EQ", expressionText, fileName, lineNumber);  // DEVPACK_FAILURE_PATH
    } else {
        ++state.passedChecks;  // DEVPACK_SUCCESS_PATH
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
    std::cout << "TEST SUMMARY: " << state.passedChecks << "/" << state.totalChecks
              << " checks passed (" << state.failedChecks << " failed)\n"
              << "DEVPACK_TESTS_COMPLETED checks=" << state.totalChecks
              << " failed=" << state.failedChecks << "\n"
              << std::flush;
    if (state.totalChecks == 0U || state.failedChecks > 0U || state.passedChecks != state.totalChecks) {
        return 1;  // DEVPACK_FAILURE_PATH
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

/**
 * @brief Intrusive linked-list node for self-registering test suites in separate files.
 */
struct TestSuiteNode {
    /** @brief Test suite entry function pointer. */
    void (*run)();
    /** @brief Next registered node in the intrusive list. */
    TestSuiteNode* next;
};

/**
 * @brief Return the head pointer reference for self-registered test suites.
 * @tparam Tag Dummy template parameter to keep function header-only under lint rules.
 * @return Reference to head pointer of registered test suites.
 */
template <int Tag = 0>
inline TestSuiteNode*& registeredSuiteHead() noexcept {
    static TestSuiteNode* head = nullptr;
    return head;
}

/**
 * @brief Execute all self-registered test suites in registration order.
 * @tparam Tag Dummy template parameter to keep function header-only under lint rules.
 */
template <int Tag = 0>
inline void runRegisteredTestSuites() {
    for (const TestSuiteNode* node = registeredSuiteHead<Tag>(); node != nullptr; node = node->next) {
        if (node->run != nullptr) {
            node->run();
        }
    }
}

/**
 * @brief Static registrar adding a test suite function to the global intrusive list.
 * @tparam Tag Dummy template parameter to keep definition header-only under lint rules.
 */
template <int Tag = 0>
struct AutoTestSuiteRegistrar {
    /**
     * @brief Register @p suiteFn on static initialization.
     * @param[in] suiteFn Test suite function to invoke from test_main.
     */
    explicit AutoTestSuiteRegistrar(void (*suiteFn)()) noexcept : node{suiteFn, nullptr} {
        node.next = registeredSuiteHead<Tag>();
        registeredSuiteHead<Tag>() = &node;
    }

    /** @brief Embedded intrusive node for this suite. */
    TestSuiteNode node;
};

}  // namespace can_fixture_test

#define REGISTER_TEST_SUITE(fn) \
    static ::can_fixture_test::AutoTestSuiteRegistrar<0> kSuiteReg##fn(&(fn))

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
