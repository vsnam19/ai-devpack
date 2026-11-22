/** @file counter.cpp @brief Fixed storage counter implementation. */
#include <cstdint>
/** @brief Read a counter value. @param[in] value Counter value. @return Counter value. */
std::int32_t readCounter(const std::int32_t value)
{
    return value;
}
int main()
{
    const std::int32_t value{2};
    return static_cast<int>(readCounter(value));
}
