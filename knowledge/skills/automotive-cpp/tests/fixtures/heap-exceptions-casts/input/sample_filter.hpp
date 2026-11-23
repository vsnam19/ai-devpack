#ifndef SAMPLE_FILTER_HPP
#define SAMPLE_FILTER_HPP

#include <cstddef>
#include <cstdint>

// Fixed-capacity rolling integer average filter.
// Contract to preserve:
// - kMaxWindowSize = 8U
// - SampleFilter(std::size_t windowSize): if windowSize == 0 or > kMaxWindowSize, clamp to [1, kMaxWindowSize]
// - pushSample(std::int32_t sample): returns true on success
// - computeAverage(std::int32_t& averageOut): returns false if empty, else true with integer mean
// - sampleCount(): number of stored samples currently in window
// - reset(): clears stored samples
class SampleFilter {
public:
    static constexpr std::size_t kMaxWindowSize = 8U;

    SampleFilter(std::size_t windowSize);
    ~SampleFilter();

    bool pushSample(std::int32_t sample);
    bool computeAverage(std::int32_t& averageOut) const;
    std::size_t sampleCount() const;
    void reset();

private:
    std::int32_t* m_samples;
    std::size_t m_windowSize;
    std::size_t m_headIndex;
    std::size_t m_sampleCount;
};

#endif
