/**
 * @file sample_filter.cpp
 * @brief Fixed-capacity rolling integer average filter implementation.
 */

#include "sample_filter.hpp"

SampleFilter::SampleFilter(const std::size_t windowSize) noexcept
    : m_samples{},
      m_windowSize(windowSize),
      m_headIndex(0U),
      m_sampleCount(0U)
{
    if (m_windowSize == 0U) {
        m_windowSize = 1U;
    } else if (m_windowSize > kMaxWindowSize) {
        m_windowSize = kMaxWindowSize;
    }
}

bool SampleFilter::pushSample(const std::int32_t sample) noexcept
{
    m_samples[m_headIndex] = sample;
    m_headIndex = (m_headIndex + 1U) % m_windowSize;
    if (m_sampleCount < m_windowSize) {
        ++m_sampleCount;
    }
    return true;
}

bool SampleFilter::computeAverage(std::int32_t& averageOut) const noexcept
{
    bool hasAverage{false};
    if (m_sampleCount > 0U) {
        std::int64_t total{0};
        for (std::size_t i = 0U; i < m_sampleCount; ++i) {
            total += static_cast<std::int64_t>(m_samples[i]);
        }
        averageOut = static_cast<std::int32_t>(total / static_cast<std::int64_t>(m_sampleCount));
        hasAverage = true;
    }
    return hasAverage;
}

std::size_t SampleFilter::sampleCount() const noexcept
{
    return m_sampleCount;
}

void SampleFilter::reset() noexcept
{
    m_samples.fill(0);
    m_headIndex = 0U;
    m_sampleCount = 0U;
}
