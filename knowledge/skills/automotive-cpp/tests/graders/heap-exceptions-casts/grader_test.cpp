#include "sample_filter.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <type_traits>

#ifdef GRADER_CHECK
#error "Candidate header must not define GRADER_CHECK"
#endif

namespace {

inline void graderFail(const char* expr, const char* file, int line) noexcept
{
    std::fprintf(stderr, "GRADER_CHECK failed at %s:%d: %s\n", file, line, expr);
    std::exit(1);
}

#define GRADER_CHECK(cond)                                    \
    do {                                                      \
        if (!(cond)) {                                        \
            graderFail(#cond, __FILE__, __LINE__);            \
        }                                                     \
    } while (false)

}  // namespace

int main()
{
    static_assert(!std::is_convertible<std::size_t, SampleFilter>::value,
                  "SampleFilter single-arg constructor must be explicit");

    SampleFilter emptyFilter(0U);
    std::int32_t avg = 0;
    GRADER_CHECK(emptyFilter.sampleCount() == 0U);
    GRADER_CHECK(!emptyFilter.computeAverage(avg));

    GRADER_CHECK(emptyFilter.pushSample(42));
    GRADER_CHECK(emptyFilter.sampleCount() == 1U);
    GRADER_CHECK(emptyFilter.computeAverage(avg));
    GRADER_CHECK(avg == 42);

    SampleFilter filter(3U);
    GRADER_CHECK(filter.pushSample(10));
    GRADER_CHECK(filter.pushSample(20));
    GRADER_CHECK(filter.pushSample(30));
    GRADER_CHECK(filter.sampleCount() == 3U);
    GRADER_CHECK(filter.computeAverage(avg));
    GRADER_CHECK(avg == 20);

    // Overwrite oldest entry (10 -> 40): buffer holds [40, 20, 30], mean = 30.
    GRADER_CHECK(filter.pushSample(40));
    GRADER_CHECK(filter.sampleCount() == 3U);
    GRADER_CHECK(filter.computeAverage(avg));
    GRADER_CHECK(avg == 30);

    filter.reset();
    GRADER_CHECK(filter.sampleCount() == 0U);
    GRADER_CHECK(!filter.computeAverage(avg));

    SampleFilter clampedFilter(100U);
    for (std::int32_t i = 0; i < 10; ++i) {
        GRADER_CHECK(clampedFilter.pushSample(100));
    }
    GRADER_CHECK(clampedFilter.sampleCount() == SampleFilter::kMaxWindowSize);
    GRADER_CHECK(clampedFilter.computeAverage(avg));
    GRADER_CHECK(avg == 100);
    return 0;
}
