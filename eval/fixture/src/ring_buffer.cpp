/**
 * @file ring_buffer.cpp
 * @brief Implementation of the fixed-capacity FrameRingBuffer class.
 */

#include "ring_buffer.hpp"

namespace can_fixture {

namespace {

/**
 * @brief Clamp a requested ring capacity into [1, kMaxRingCapacity].
 * @param[in] requestedCapacity Caller-requested capacity.
 * @return Clamped capacity in [1, kMaxRingCapacity].
 */
std::size_t clampCapacity(std::size_t requestedCapacity) noexcept {
    if (requestedCapacity == 0U) {
        return 1U;
    }
    if (requestedCapacity > kMaxRingCapacity) {
        return kMaxRingCapacity;
    }
    return requestedCapacity;
}

}  // namespace

FrameRingBuffer::FrameRingBuffer(std::size_t capacity) noexcept
    : m_storage{},
      m_capacity(clampCapacity(capacity)),
      m_head(0U),
      m_tail(0U),
      m_count(0U),
      m_droppedCount(0U) {
}

bool FrameRingBuffer::push(const CanFrame& frame) noexcept {
    if (m_count >= m_capacity) {
        return false;
    }
    m_storage[m_head] = frame;
    m_head = (m_head + 1U) % m_capacity;
    ++m_count;
    return true;
}

bool FrameRingBuffer::pushOverwrite(const CanFrame& frame) noexcept {
    const bool wasFull = (m_count >= m_capacity);
    m_storage[m_head] = frame;
    m_head = (m_head + 1U) % m_capacity;
    if (wasFull) {
        m_tail = (m_tail + 1U) % m_capacity;
        ++m_droppedCount;
    } else {
        ++m_count;
    }
    return wasFull;
}

bool FrameRingBuffer::pop(CanFrame* frameOut) noexcept {
    if (frameOut == nullptr || m_count == 0U) {
        return false;
    }
    *frameOut = m_storage[m_tail];
    m_tail = (m_tail + 1U) % m_capacity;
    --m_count;
    return true;
}

bool FrameRingBuffer::peek(CanFrame* frameOut) const noexcept {
    if (frameOut == nullptr || m_count == 0U) {
        return false;
    }
    *frameOut = m_storage[m_tail];
    return true;
}

void FrameRingBuffer::clear() noexcept {
    m_head = 0U;
    m_tail = 0U;
    m_count = 0U;
    m_droppedCount = 0U;
}

std::size_t FrameRingBuffer::size() const noexcept {
    return m_count;
}

std::size_t FrameRingBuffer::capacity() const noexcept {
    return m_capacity;
}

std::size_t FrameRingBuffer::droppedCount() const noexcept {
    return m_droppedCount;
}

bool FrameRingBuffer::isEmpty() const noexcept {
    return m_count == 0U;
}

bool FrameRingBuffer::isFull() const noexcept {
    return m_count >= m_capacity;
}

}  // namespace can_fixture
