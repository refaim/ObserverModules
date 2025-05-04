cmake_minimum_required(VERSION 3.31)

set(VCPKG_CRT_LINKAGE static)
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_TARGET_TRIPLET x64-windows-static)

project(observer_modules LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_compile_options("/W3" "/analyze")
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
set(CMAKE_VERBOSE_MAKEFILE ON)

set(ZLIB_USE_STATIC_LIBS ON)
find_package(ZLIB REQUIRED)
find_path(ZSTR_INCLUDE_DIRS "zstr.hpp")
find_package(Python3 COMPONENTS Development REQUIRED)

add_library(zanzarah SHARED src/dll.cpp src/archive.cpp src/modules/zanzarah/module.cpp)
set_target_properties(zanzarah PROPERTIES SUFFIX ".so" PREFIX "" LINK_FLAGS "/DEF:${CMAKE_CURRENT_SOURCE_DIR}/src/modules/zanzarah/module.def")

set(RENPY_DIR src/modules/renpy)
add_library(renpy SHARED
        src/dll.cpp
        src/archive.cpp
        ${RENPY_DIR}/module.cpp
        ${RENPY_DIR}/python/context.cpp
        ${RENPY_DIR}/python/dict.cpp
        ${RENPY_DIR}/python/list.cpp
        ${RENPY_DIR}/python/tuple.cpp)
set_target_properties(renpy PROPERTIES SUFFIX ".so" PREFIX "" LINK_FLAGS "/DEF:${CMAKE_CURRENT_SOURCE_DIR}/src/modules/renpy/module.def")
target_link_libraries(renpy PRIVATE ZLIB::ZLIB)
target_include_directories(renpy PRIVATE ${ZSTR_INCLUDE_DIRS})
target_link_libraries(renpy PRIVATE Python3::Python)
