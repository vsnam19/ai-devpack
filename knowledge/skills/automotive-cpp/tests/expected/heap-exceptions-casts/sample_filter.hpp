/**
 * @file sample_filter.hpp
 * @brief Fixed-capacity rolling integer average filter interface.
 */

#ifndef SAMPLE_FILTER_HPP
#define SAMPLE_FILTER_HPP

#include <array>
#include <cstddef>
#include <cstdint>

/**
 * @brief Bounded rolling-window average filter using fixed storage.
 */
class SampleFilter {
public:
    /** @brief Maximum supported rolling window length. */
    static constexpr std::size_t kMaxWindowSize = 8U;

    /**
     * @brief Construct a rolling filter with a clamped window size.
     * @param[in] windowSize Requested window size (clamped to [1, kMaxWindowSize]).
     */
    explicit SampleFilter(std::size_t windowSize) noexcept;

    /**
     * @brief Push a new sample into the rolling window, overwriting the oldest entry when full.
     * @param[in] sample Signed 32-bit sample value to store.
     * @return True when the sample is recorded.
     */
    bool pushSample(std::int32_t sample) noexcept;

    /**
     * @brief Compute the integer mean of currently buffered samples.
     * @param[out] averageOut Computed integer average when the filter is non-empty.
     * @return True if at least one sample is buffered, false when empty.
     */
    bool computeAverage(std::int32_t& averageOut) const noexcept;

    /**
     * @brief Return the number of samples currently buffered.
     * @return Buffered sample count in [0, windowSize].
     */
    std::size_t sampleCount() const noexcept;

    /**
     * @brief Clear all buffered samples and reset the write index.
     */
    void reset() noexcept;

private:
    std::array<std::int32_t, kMaxWindowSize> m_samples;
    std::size_t m_windowSize;
    std::size_t m_headIndex;
    std::size_t m_sampleCount;
};

#endif
