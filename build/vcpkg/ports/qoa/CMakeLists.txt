cmake_minimum_required(VERSION 3.20)
project(qoa LANGUAGES C)

add_library(qoa STATIC qoa.c)
target_include_directories(qoa PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
install(TARGETS qoa ARCHIVE DESTINATION lib)
install(FILES qoa.h DESTINATION include)
