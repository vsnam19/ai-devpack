/**
 * @file diag_svc.hpp
 * @brief Diagnostic service fault handler.
 */
#pragma once

#include <cstdint>

namespace org {

/** @brief Handle component diagnostic fault codes. */
class DiagManager {
public:
    /**
     * @brief Process a diagnostic code.
     * @param[in] faultCode Fault identifier to evaluate.
     * @return Encoded fault status.
     */
    std::int32_t handleFault(std::int32_t faultCode) const noexcept;
};

}  // namespace org
