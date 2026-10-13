# Overlay port: the official zlib-ng port (ports/zlib-ng) rebuilt in zlib
# compatibility mode and published under the `zlib` package identity.  Every
# consumer -- libpng and the Ren'Py zlib codec alike -- therefore resolves the
# single zlib-ng 2.3.3 provider instead of vendoring madler zlib beside it.
# The library keeps its real version (2.3.3) and license (Zlib); only the
# compat ABI/package name is reused.

vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO zlib-ng/zlib-ng
    REF "${VERSION}"
    SHA512 e2057c764f1d5aaee738edee7e977182c5b097e3c95489dcd8de813f237d92a05daaa86d68d44b331d9fec5d1802586a8f6cfb658ba849874aaa14e72a8107f5
    HEAD_REF develop
)

# ZLIB_COMPAT is fixed on here: exposing the classic zlib API/ABI is this
# provider's whole purpose, so unlike the upstream zlib-ng port it is not left
# for the triplet to toggle.
set(ZLIB_COMPAT ON)

vcpkg_cmake_configure(
    SOURCE_PATH "${SOURCE_PATH}"
    OPTIONS
        -DZLIB_ENABLE_TESTS=OFF
        -DWITH_NEW_STRATEGIES=ON
        -DZLIB_COMPAT=ON
    OPTIONS_RELEASE
        -DWITH_OPTIM=ON
)
vcpkg_cmake_install()
vcpkg_copy_pdbs()

# On Windows MSVC the compat static archive is `zlibstatic` (see zlib-ng
# CMakeLists OUTPUT_NAME) while the generated pkg-config file advertises plain
# `-lz`; realign the .pc files with the artifact that is actually installed.
# Dynamic builds keep the upstream `zlib` output name.
if(VCPKG_TARGET_IS_WINDOWS AND (NOT (VCPKG_LIBRARY_LINKAGE STREQUAL static AND VCPKG_TARGET_IS_MINGW)))
    if(VCPKG_LIBRARY_LINKAGE STREQUAL "dynamic")
        set(_port_output_name "zlib")
    else()
        set(_port_output_name "zlibstatic")
    endif()

    if(NOT DEFINED VCPKG_BUILD_TYPE OR VCPKG_BUILD_TYPE STREQUAL "release")
        vcpkg_replace_string("${CURRENT_PACKAGES_DIR}/lib/pkgconfig/zlib.pc" " -lz" " -l${_port_output_name}")
    endif()
    if(NOT DEFINED VCPKG_BUILD_TYPE OR VCPKG_BUILD_TYPE STREQUAL "debug")
        vcpkg_replace_string("${CURRENT_PACKAGES_DIR}/debug/lib/pkgconfig/zlib.pc" " -lz" " -l${_port_output_name}d")
    endif()
endif()

vcpkg_fixup_pkgconfig()

# zlib-ng installs its compat CMake package under lib/cmake/ZLIB.
vcpkg_cmake_config_fixup(CONFIG_PATH lib/cmake/ZLIB)

file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/share"
                    "${CURRENT_PACKAGES_DIR}/debug/include"
)

# Module-mode find_package(ZLIB), which the archive build uses, resolves the
# compat artifacts through the official zlib port's wrapper protocol.
file(INSTALL "${CMAKE_CURRENT_LIST_DIR}/vcpkg-cmake-wrapper.cmake" DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}")
file(COPY "${CMAKE_CURRENT_LIST_DIR}/usage" DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}")

vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/LICENSE.md")
