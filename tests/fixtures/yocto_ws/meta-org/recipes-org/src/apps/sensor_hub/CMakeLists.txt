cmake_minimum_required(VERSION 3.16)
project(sensor_hub LANGUAGES CXX)
add_library(sensor_hub STATIC src/sensor_hub.cpp)
target_include_directories(sensor_hub PUBLIC include)
