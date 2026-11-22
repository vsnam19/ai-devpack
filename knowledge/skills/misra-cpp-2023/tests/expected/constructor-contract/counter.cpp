/** @file counter.cpp @brief Counter value implementation. */
#include <cstdint>
/** @brief Store a fixed-width counter value. */
class Counter
{
public:
    /** @brief Construct a counter. @param[in] value Initial counter value. */
    explicit Counter(const std::int32_t value) : m_value(value) {}
    /** @brief Get the stored counter value. @return Stored counter value. */
    std::int32_t value() const { return m_value; }
private:
    std::int32_t m_value;
};
int main()
{
    const Counter counter{4};
    return static_cast<int>(counter.value());
}
