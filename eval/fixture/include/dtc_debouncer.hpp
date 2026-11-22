/**
 * @file dtc_debouncer.hpp
 * @brief Diagnostic Trouble Code (DTC) counter-based fault debouncer with operation-cycle aging.
 */

#ifndef CAN_FIXTURE_DTC_DEBOUNCER_HPP
#define CAN_FIXTURE_DTC_DEBOUNCER_HPP

#include <cstdint>

namespace can_fixture {

/**
 * @brief Maximum absolute counter magnitude allowed for DtcConfig thresholds and steps.
 */
constexpr std::int16_t kMaxDebounceThreshold = 16383;

/**
 * @brief Qualification and healing state of a supervised diagnostic trouble code.
 */
enum class DtcState : std::uint8_t {
    /** @brief No fault is pending or confirmed (counter <= 0 and not latched). */
    Inactive = 0U,
    /** @brief Fault counter is positive (counter > 0) but has not yet reached confirmThreshold. */
    Pending = 1U,
    /** @brief Fault counter reached confirmThreshold and fault is actively latched. */
    Confirmed = 2U,
    /** @brief Latched fault returned to clearThreshold and completed at least one aging cycle. */
    Healing = 3U
};

/**
 * @brief Counter thresholds, step sizes, and aging parameters for DtcDebouncer.
 */
struct DtcConfig {
    /** @brief Positive fault counter threshold required to confirm a fault (1..kMaxDebounceThreshold). */
    std::int16_t confirmThreshold;
    /** @brief Non-positive counter floor reached on pass samples (-kMaxDebounceThreshold..0). */
    std::int16_t clearThreshold;
    /** @brief Positive counter increment applied on each failed diagnostic sample (1..kMaxDebounceThreshold). */
    std::int16_t failStep;
    /** @brief Positive counter decrement applied on each passed diagnostic sample (1..kMaxDebounceThreshold). */
    std::int16_t passStep;
    /** @brief Consecutive fault-free operation cycles at clearThreshold required to heal (> 0). */
    std::uint8_t agingThreshold;
};

/**
 * @brief Stateful diagnostic monitor debouncer managing fault confirmation and multi-cycle aging.
 */
class DtcDebouncer {
public:
    /**
     * @brief Construct a diagnostic debouncer in the Inactive state with zeroed counters.
     * @param[in] config Threshold, step size, and aging configuration parameters.
     */
    explicit DtcDebouncer(const DtcConfig& config) noexcept;

    /**
     * @brief Validate that thresholds and step sizes lie within allowed bounds.
     * @return True if confirmThreshold, failStep, passStep are in [1, kMaxDebounceThreshold],
     *         clearThreshold is in [-kMaxDebounceThreshold, 0], and agingThreshold > 0.
     */
    bool isConfigValid() const noexcept;

    /**
     * @brief Process a single pass/fail diagnostic sample and update debounce state.
     * @param[in] isFaultPresent True if the sample detected a fault condition, false if passed.
     * @return Updated DtcState after applying failStep or passStep.
     */
    DtcState reportSample(bool isFaultPresent) noexcept;

    /**
     * @brief Evaluate end-of-operation-cycle aging when a confirmed fault is at clearThreshold.
     * @return True if an aging step was recorded (transitioning to Healing or Inactive),
     *         false if config is invalid, fault is not latched, or counter > clearThreshold.
     */
    bool onOperationCycleEnd() noexcept;

    /**
     * @brief Reset the debouncer back to Inactive and clear all counters and latched status.
     */
    void reset() noexcept;

    /**
     * @brief Return the current diagnostic qualification state.
     * @return Current DtcState value.
     */
    DtcState state() const noexcept;

    /**
     * @brief Return the current internal fault debounce counter.
     * @return Signed debounce counter in [clearThreshold, confirmThreshold].
     */
    std::int16_t counter() const noexcept;

    /**
     * @brief Return the number of completed consecutive fault-free operation cycles during healing.
     * @return Healing counter in [0, agingThreshold).
     */
    std::uint8_t healingCounter() const noexcept;

    /**
     * @brief Check whether the fault is currently latched (Confirmed or Healing).
     * @return True if the fault has reached confirmation and has not yet fully aged out.
     */
    bool isConfirmed() const noexcept;

private:
    DtcConfig m_config;
    DtcState m_state;
    std::int16_t m_counter;
    std::uint8_t m_healingCounter;
    bool m_isLatched;
};

}  // namespace can_fixture

#endif  // CAN_FIXTURE_DTC_DEBOUNCER_HPP
