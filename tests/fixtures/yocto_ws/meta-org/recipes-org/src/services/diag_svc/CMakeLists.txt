cmake_minimum_required(VERSION 3.16)
project(diag_svc LANGUAGES CXX)
add_library(diag_svc STATIC src/diag_svc.cpp)
target_include_directories(diag_svc PUBLIC include)
