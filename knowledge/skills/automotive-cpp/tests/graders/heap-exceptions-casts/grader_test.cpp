#include "sample_filter.hpp"

#include <cassert>
#include <cstdint>
#include <type_traits>

int main()
{
    static_assert(!std::is_convertible<std::size_t, SampleFilter>::value,
                  "SampleFilter single-arg constructor must be explicit");

    SampleFilter emptyFilter(0U);
    std::int32_t avg = 0;
    assert(emptyFilter.sampleCount() == 0U);
    assert(!emptyFilter.computeAverage(avg));

    assert(emptyFilter.pushSample(42));
    assert(emptyFilter.sampleCount() == 1U);
    assert(emptyFilter.computeAverage(avg));
    assert(avg == 42);

    SampleFilter filter(3U);
    assert(filter.pushSample(10));
    assert(filter.pushSample(20));
    assert(filter.pushSample(30));
    assert(filter.sampleCount() == 3U);
    assert(filter.computeAverage(avg));
    assert(avg == 20);

    // Overwrite oldest entry (10 -> 40): buffer holds [40, 20, 30], mean = 30.
    assert(filter.pushSample(40));
    assert(filter.sampleCount() == 3U);
    assert(filter.computeAverage(avg));
    assert(avg == 30);

    filter.reset();
    assert(filter.sampleCount() == 0U);
    assert(!filter.computeAverage(avg));

    SampleFilter clampedFilter(100U);
    for (std::int32_t i = 0; i < 10; ++i) {
        assert(clampedFilter.pushSample(100));
    }
    assert(clampedFilter.sampleCount() == SampleFilter::kMaxWindowSize);
    assert(clampedFilter.computeAverage(avg));
    assert(avg == 100);
    return 0;
}
