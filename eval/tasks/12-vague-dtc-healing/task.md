# Task: 12-vague-dtc-healing

Category: vague-bugfix
Tier: v2

## Goal
A diagnostic may remain pending after enough clean operation cycles. Determine the intended aging behavior from REQ-DTC-003 and correct it.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
- tests/test_dtc_debouncer.cpp

## Commands
- `bash build_and_test.sh`

## STOP rule
- Do not weaken tests or change files outside scope.
