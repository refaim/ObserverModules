# The header bytes are identical to the previously tested third_party/bcdec/bcdec.h.
vcpkg_download_distfile(BCDEC_HEADER
    URLS "https://raw.githubusercontent.com/iOrange/bcdec/80859ed3b7afb1c527a2a99d70c61457bea72d0c/bcdec.h"
    FILENAME "bcdec-80859ed3b7afb1c527a2a99d70c61457bea72d0c.h"
    SHA512 17913d49e64755ba3b0a845352e0f36bcda6e80e653ab70c3c27ae7a3510d6a6f621a68ce374075fe67d1487d211f5f9e322e8f81817b4480f3f2eef5b4e5390
)

set(SOURCE_PATH "${CURRENT_BUILDTREES_DIR}/source")
file(MAKE_DIRECTORY "${SOURCE_PATH}")
file(COPY_FILE "${BCDEC_HEADER}" "${SOURCE_PATH}/bcdec.h")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/bcdec.c" "${SOURCE_PATH}/bcdec.c")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/CMakeLists.txt" "${SOURCE_PATH}/CMakeLists.txt")

vcpkg_cmake_configure(SOURCE_PATH "${SOURCE_PATH}")
vcpkg_cmake_install()
vcpkg_copy_pdbs()
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/include" "${CURRENT_PACKAGES_DIR}/debug/share")
vcpkg_install_copyright(FILE_LIST "${CMAKE_CURRENT_LIST_DIR}/copyright")
