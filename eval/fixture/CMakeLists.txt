cmake_minimum_required(VERSION 3.14)
project(can_fixture LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_compile_options(
    -Wall
    -Wextra
    -Werror
    -Wpedantic
    -Wswitch-enum
    -Wunreachable-code
    -fno-exceptions
    -fno-rtti
)

add_library(can_fixture_lib STATIC
    src/can_codec.cpp
    src/ring_buffer.cpp
    src/link_state_machine.cpp
    src/dtc_debouncer.cpp
)
target_include_directories(can_fixture_lib PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

file(GLOB FIXTURE_TEST_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/*.cpp"
)
add_executable(fixture_tests
    ${FIXTURE_TEST_SOURCES}
)
target_include_directories(fixture_tests PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/tests
)
target_link_libraries(fixture_tests PRIVATE can_fixture_lib)
if(DEFINED ENV{DEVPACK_ROOT})
    set(DEVPACK_FRAMEWORK "$ENV{DEVPACK_ROOT}/eval/fixture/tests/test_framework.hpp")
else()
    set(DEVPACK_FRAMEWORK "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_framework.hpp")
endif()
string(RANDOM LENGTH 24 ALPHABET ABCDEF0123456789 DEVPACK_GUARD_SUFFIX)
target_compile_options(fixture_tests PRIVATE
    --coverage -O0
    "-DDEVPACK_FRAMEWORK_GUARD=DEVPACK_TF_${DEVPACK_GUARD_SUFFIX}"
    -include "${DEVPACK_FRAMEWORK}"
)
target_link_options(fixture_tests PRIVATE --coverage)

enable_testing()
add_test(NAME fixture_tests COMMAND fixture_tests)
