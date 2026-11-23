cmake_minimum_required(VERSION 3.16)
project(ipc_bus LANGUAGES CXX)
add_library(ipc_bus STATIC src/ipc_bus.cpp)
target_include_directories(ipc_bus PUBLIC include)
