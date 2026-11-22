/**
 * @file dtc_debouncer.cpp
 * @brief Implementation of the DtcDebouncer fault qualification and aging state machine.
 */

#include "dtc_debouncer.hpp"

namespace can_fixture {

DtcDebouncer::DtcDebouncer(const DtcConfig& config) noexcept
    : m_config(config),
      m_state(DtcState::Inactive),
      m_counter(0),
      m_healingCounter(0U),
      m_isLatched(false) {
}

bool DtcDebouncer::isConfigValid() const noexcept {
    const bool confirmValid =
        (m_config.confirmThreshold > 0) && (m_config.confirmThreshold <= kMaxDebounceThreshold);
    const bool clearValid =
        (m_config.clearThreshold <= 0) && (m_config.clearThreshold >= -kMaxDebounceThreshold);
    const bool stepsValid =
        (m_config.failStep > 0) && (m_config.failStep <= kMaxDebounceThreshold) &&
        (m_config.passStep > 0) && (m_config.passStep <= kMaxDebounceThreshold);
    return confirmValid && clearValid && stepsValid && (m_config.agingThreshold > 0U);
}

DtcState DtcDebouncer::reportSample(bool isFaultPresent) noexcept {
    if (!isConfigValid()) {
        return m_state;
    }
    if (isFaultPresent) {
        const std::int32_t nextCounter =
            static_cast<std::int32_t>(m_counter) + static_cast<std::int32_t>(m_config.failStep);
        if (nextCounter >= static_cast<std::int32_t>(m_config.confirmThreshold)) {
            m_counter = m_config.confirmThreshold;
            m_isLatched = true;
        } else {
            m_counter = static_cast<std::int16_t>(nextCounter);
        }
        m_healingCounter = 0U;
        if (m_isLatched) {
            m_state = DtcState::Confirmed;
        } else if (m_counter > 0) {
            m_state = DtcState::Pending;
        } else {
            m_state = DtcState::Inactive;
        }
    } else {
        const std::int32_t nextCounter =
            static_cast<std::int32_t>(m_counter) - static_cast<std::int32_t>(m_config.passStep);
        if (nextCounter <= static_cast<std::int32_t>(m_config.clearThreshold)) {
            m_counter = m_config.clearThreshold;
        } else {
            m_counter = static_cast<std::int16_t>(nextCounter);
        }
        if (!m_isLatched) {
            if (m_counter > 0) {
                m_state = DtcState::Pending;
            } else {
                m_state = DtcState::Inactive;
            }
        }
    }
    return m_state;
}

bool DtcDebouncer::onOperationCycleEnd() noexcept {
    if (!isConfigValid() || !m_isLatched || m_counter > m_config.clearThreshold) {
        return false;
    }
    ++m_healingCounter;
    if (m_healingCounter >= m_config.agingThreshold) {
        m_isLatched = false;
        m_healingCounter = 0U;
        m_counter = 0;
        m_state = DtcState::Inactive;
    } else {
        m_state = DtcState::Healing;
    }
    return true;
}

void DtcDebouncer::reset() noexcept {
    m_state = DtcState::Inactive;
    m_counter = 0;
    m_healingCounter = 0U;
    m_isLatched = false;
}

DtcState DtcDebouncer::state() const noexcept {
    return m_state;
}

std::int16_t DtcDebouncer::counter() const noexcept {
    return m_counter;
}

std::uint8_t DtcDebouncer::healingCounter() const noexcept {
    return m_healingCounter;
}

bool DtcDebouncer::isConfirmed() const noexcept {
    return m_isLatched;
}

}  // namespace can_fixture
