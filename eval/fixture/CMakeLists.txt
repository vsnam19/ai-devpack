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
)
target_include_directories(can_fixture_lib PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

add_executable(fixture_tests
    tests/test_main.cpp
    tests/test_can_codec.cpp
    tests/test_ring_buffer.cpp
    tests/test_link_state_machine.cpp
)
target_include_directories(fixture_tests PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/tests
)
target_link_libraries(fixture_tests PRIVATE can_fixture_lib)

enable_testing()
add_test(NAME fixture_tests COMMAND fixture_tests)
