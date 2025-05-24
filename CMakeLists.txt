cmake_minimum_required(VERSION 3.31)

if (DEFINED ENV{VCPKG_ROOT})
    set(VCPKG_ROOT "$ENV{VCPKG_ROOT}")
elseif (DEFINED ENV{USERPROFILE})
    set(VCPKG_ROOT "$ENV{USERPROFILE}/.vcpkg-clion/vcpkg")
endif ()
if (NOT EXISTS ${VCPKG_ROOT})
    message(FATAL_ERROR "VCPKG_ROOT is not defined. Please set it to the path of your vcpkg installation.")
endif ()
file(TO_CMAKE_PATH ${VCPKG_ROOT} VCPKG_ROOT)
set(CMAKE_TOOLCHAIN_FILE "${VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake")

set(VCPKG_CRT_LINKAGE static)
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_TARGET_TRIPLET ${OBSERVER_ARCHITECTURE}-windows-static)

project(observer_modules LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options("/W3" "/analyze")
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

set(ZLIB_USE_STATIC_LIBS ON)
find_package(ZLIB REQUIRED)
find_path(ZSTR_INCLUDE_DIRS "zstr.hpp")

set(ALL_MODULES renpy rpgmaker zanzarah)
set(RELEASED_MODULES renpy rpgmaker zanzarah)

set(RENPY_DIR src/modules/renpy)
add_library(renpy SHARED
        src/dll.cpp
        src/archive.cpp
        ${RENPY_DIR}/renpy.cpp
        ${RENPY_DIR}/pickle.cpp
)
target_link_libraries(renpy PRIVATE ZLIB::ZLIB)
target_include_directories(renpy PRIVATE ${ZSTR_INCLUDE_DIRS})

add_library(rpgmaker SHARED src/dll.cpp src/archive.cpp src/modules/rpgmaker/rpgmaker.cpp)

add_library(zanzarah SHARED src/dll.cpp src/archive.cpp src/modules/zanzarah/zanzarah.cpp)

foreach (module IN LISTS ALL_MODULES)
    set_target_properties(${module} PROPERTIES SUFFIX ".so" PREFIX "" LINK_FLAGS "/DEF:${CMAKE_CURRENT_SOURCE_DIR}/src/modules/${module}/${module}.def")
endforeach ()

# === CTest ===

find_package(Catch2 REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(xxHash CONFIG REQUIRED)
add_executable(tests
        src/tests/framework/observer.cpp
        src/tests/framework/testcase.cpp
        src/tests/renpy.cpp
        src/tests/rpgmaker.cpp
        src/tests/zanzarah.cpp
)
target_link_libraries(tests PRIVATE Catch2::Catch2WithMain)
target_link_libraries(tests PRIVATE nlohmann_json::nlohmann_json)
target_link_libraries(tests PRIVATE xxHash::xxhash)
target_link_libraries(tests PRIVATE ${ALL_MODULES})
include(CTest)
include(Catch)
catch_discover_tests(tests)

# === CPack ===

file(MAKE_DIRECTORY ${CMAKE_BINARY_DIR}/docs_temp/thirdparty)
file(COPY ${CMAKE_SOURCE_DIR}/licenses/ DESTINATION ${CMAKE_BINARY_DIR}/docs_temp/thirdparty)

string(TIMESTAMP TODAY "%Y-%m-%d")
foreach (MODULE IN LISTS RELEASED_MODULES)
    install(TARGETS ${MODULE} RUNTIME DESTINATION . COMPONENT ${MODULE})
    install(FILES ${CMAKE_SOURCE_DIR}/src/modules/${MODULE}/observer_user.ini DESTINATION . COMPONENT ${MODULE})

    install(FILES ${CMAKE_SOURCE_DIR}/LICENSE.txt DESTINATION docs RENAME license.txt COMPONENT ${MODULE})
    install(DIRECTORY ${CMAKE_BINARY_DIR}/docs_temp/ DESTINATION docs COMPONENT ${MODULE})

    install(FILES "$<TARGET_PDB_FILE:${MODULE}>" DESTINATION . COMPONENT ${MODULE}_pdb)

    string(TOUPPER ${MODULE} MODULE_UPPER)
    set(CPACK_ARCHIVE_${MODULE_UPPER}_FILE_NAME "${MODULE}-${TODAY}-${OBSERVER_ARCHITECTURE}-dll")
    set(CPACK_ARCHIVE_${MODULE_UPPER}_PDB_FILE_NAME "${MODULE}-${TODAY}-${OBSERVER_ARCHITECTURE}-pdb")
endforeach ()

list(TRANSFORM RELEASED_MODULES APPEND "_pdb" OUTPUT_VARIABLE ALL_COMPONENTS)
list(PREPEND ALL_COMPONENTS ${RELEASED_MODULES})

set(CPACK_GENERATOR ZIP)
set(CPACK_ARCHIVE_COMPONENT_INSTALL ON)
set(CPACK_COMPONENTS_ALL ${ALL_COMPONENTS})

include(CPack)