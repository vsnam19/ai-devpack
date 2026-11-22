/**
 * @file ring_buffer.hpp
 * @brief Fixed-capacity CAN frame ring buffer without dynamic heap allocation.
 */

#ifndef CAN_FIXTURE_RING_BUFFER_HPP
#define CAN_FIXTURE_RING_BUFFER_HPP

#include <cstddef>
#include <cstdint>

#include "can_codec.hpp"

namespace can_fixture {

/**
 * @brief Maximum compile-time capacity of a FrameRingBuffer instance.
 */
constexpr std::size_t kMaxRingCapacity = 16U;

/**
 * @brief Classical CAN frame representation with identifier, data length code, and payload.
 */
struct CanFrame {
    /** @brief 11-bit or 29-bit CAN identifier. */
    std::uint32_t canId;
    /** @brief Data length code (0..8 bytes). */
    std::uint8_t dlc;
    /** @brief Raw frame payload bytes. */
    std::uint8_t payload[kMaxFrameBytes];
};

/**
 * @brief Fixed-capacity FIFO ring buffer for CanFrame elements.
 */
class FrameRingBuffer {
public:
    /**
     * @brief Construct a ring buffer with a bounded active capacity.
     * @param[in] capacity Requested capacity (clamped to [1, kMaxRingCapacity]).
     */
    explicit FrameRingBuffer(std::size_t capacity = kMaxRingCapacity) noexcept;

    /**
     * @brief Insert a frame at the tail of the buffer if space is available.
     * @param[in] frame CAN frame to enqueue.
     * @return True if the frame was stored, false if the buffer was already full.
     */
    bool push(const CanFrame& frame) noexcept;

    /**
     * @brief Insert a frame, overwriting the oldest frame if the buffer is full.
     * @param[in] frame CAN frame to enqueue.
     * @return True if an existing oldest frame was overwritten, false if inserted without drop.
     */
    bool pushOverwrite(const CanFrame& frame) noexcept;

    /**
     * @brief Remove and copy the oldest frame from the buffer.
     * @param[out] frameOut Destination pointer receiving the dequeued frame.
     * @return True if a frame was dequeued, false if the buffer is empty or frameOut is null.
     */
    bool pop(CanFrame* frameOut) noexcept;

    /**
     * @brief Copy the oldest frame without removing it from the buffer.
     * @param[out] frameOut Destination pointer receiving the oldest frame.
     * @return True if a frame was copied, false if the buffer is empty or frameOut is null.
     */
    bool peek(CanFrame* frameOut) const noexcept;

    /**
     * @brief Remove all buffered frames and reset dropped frame counters.
     */
    void clear() noexcept;

    /**
     * @brief Return the number of frames currently stored in the buffer.
     * @return Current frame count in [0, capacity()].
     */
    std::size_t size() const noexcept;

    /**
     * @brief Return the active maximum frame capacity of this buffer.
     * @return Active capacity in [1, kMaxRingCapacity].
     */
    std::size_t capacity() const noexcept;

    /**
     * @brief Return the total number of frames overwritten via pushOverwrite since construction or clear.
     * @return Count of dropped oldest frames.
     */
    std::size_t droppedCount() const noexcept;

    /**
     * @brief Check whether the buffer contains zero frames.
     * @return True if size() == 0.
     */
    bool isEmpty() const noexcept;

    /**
     * @brief Check whether the buffer has reached its active capacity.
     * @return True if size() == capacity().
     */
    bool isFull() const noexcept;

private:
    CanFrame m_storage[kMaxRingCapacity];
    std::size_t m_capacity;
    std::size_t m_head;
    std::size_t m_tail;
    std::size_t m_count;
    std::size_t m_droppedCount;
};

}  // namespace can_fixture

#endif  // CAN_FIXTURE_RING_BUFFER_HPP
