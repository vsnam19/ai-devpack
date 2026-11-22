#include <cstdint>
class Counter { public: Counter(int value) : value(value) {} int value; };
int main() { Counter counter = 4; return counter.value; }
