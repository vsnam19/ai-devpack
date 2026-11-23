#include "sample_filter.hpp"

#include <stdexcept>

SampleFilter::SampleFilter(std::size_t windowSize)
    : m_samples(nullptr),
      m_windowSize(windowSize),
      m_headIndex(0U),
      m_sampleCount(0U)
{
    if (m_windowSize == 0U) {
        m_windowSize = 1U;
    } else if (m_windowSize > kMaxWindowSize) {
        m_windowSize = kMaxWindowSize;
    }
    m_samples = new std::int32_t[m_windowSize];
}

SampleFilter::~SampleFilter()
{
    delete[] m_samples;
}

bool SampleFilter::pushSample(std::int32_t sample)
{
    if (m_samples == nullptr) {
        throw std::runtime_error("uninitialized filter");
    }
    m_samples[m_headIndex] = sample;
    m_headIndex = (m_headIndex + 1U) % m_windowSize;
    if (m_sampleCount < m_windowSize) {
        ++m_sampleCount;
    }
    return true;
}

bool SampleFilter::computeAverage(std::int32_t& averageOut) const
{
    try {
        if (m_sampleCount == 0U) {
            throw std::out_of_range("empty filter");
        }
        std::int64_t total = 0;
        for (std::size_t i = 0U; i < m_sampleCount; ++i) {
            total += m_samples[i];
        }
        averageOut = (std::int32_t)(total / (std::int64_t)m_sampleCount);
        return true;
    } catch (...) {
        return false;
    }
}

std::size_t SampleFilter::sampleCount() const
{
    return m_sampleCount;
}

void SampleFilter::reset()
{
    m_headIndex = 0U;
    m_sampleCount = 0U;
}
