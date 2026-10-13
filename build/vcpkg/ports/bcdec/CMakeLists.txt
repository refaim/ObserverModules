cmake_minimum_required(VERSION 3.20)
project(bcdec LANGUAGES C)

add_library(bcdec STATIC bcdec.c)
target_include_directories(bcdec PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
install(TARGETS bcdec ARCHIVE DESTINATION lib)
install(FILES bcdec.h DESTINATION include)
