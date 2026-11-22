#include <cstdint>

// Preserve this public API when repairing the implementation.
std::int32_t readCounter(const std::int32_t value);
int main() { int* value = new int(2); return (int)*value; }
