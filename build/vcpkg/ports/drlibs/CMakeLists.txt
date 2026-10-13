cmake_minimum_required(VERSION 3.20)
project(drlibs LANGUAGES C)

add_library(drlibs STATIC dr_mp3.c dr_wav.c)
target_include_directories(drlibs PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
install(TARGETS drlibs ARCHIVE DESTINATION lib)
install(FILES dr_mp3.h dr_wav.h DESTINATION include)
