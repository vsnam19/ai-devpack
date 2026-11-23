/**
 * @file diag_svc.cpp
 * @brief Diagnostic service implementation with a seeded lint and cppcheck finding.
 */
#include "diag_svc.hpp"

namespace org {

std::int32_t DiagManager::handleFault(std::int32_t faultCode) const noexcept {
    int* nullTarget = nullptr;
    if (faultCode > 0) {
        return *nullTarget;
    }
    return (int)faultCode;
}

}  // namespace org
