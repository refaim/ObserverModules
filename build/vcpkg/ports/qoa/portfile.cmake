# Use Godot's tested fork, not a later phoboslab/qoa header with changed decoder behavior.
vcpkg_download_distfile(QOA_HEADER
    URLS "https://raw.githubusercontent.com/godotengine/godot/ed1daf0bf001b61586d9930840f2f1394092c079/thirdparty/misc/qoa.h"
    FILENAME "godot-ed1daf0bf001b61586d9930840f2f1394092c079-qoa.h"
    SHA512 4fc49c5fa4b9af72d4e1c33c9202e227b5cbb5024ec26cb5d6a11b295af3dfdeeeb0e7fa8f4d06fecafbd7e568b21d04a1ef8edf572f3df244bb49273a692b17
)

set(SOURCE_PATH "${CURRENT_BUILDTREES_DIR}/source")
file(MAKE_DIRECTORY "${SOURCE_PATH}")
file(COPY_FILE "${QOA_HEADER}" "${SOURCE_PATH}/qoa.h")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/qoa.c" "${SOURCE_PATH}/qoa.c")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/CMakeLists.txt" "${SOURCE_PATH}/CMakeLists.txt")

vcpkg_cmake_configure(SOURCE_PATH "${SOURCE_PATH}")
vcpkg_cmake_install()
vcpkg_copy_pdbs()
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/include" "${CURRENT_PACKAGES_DIR}/debug/share")
vcpkg_install_copyright(FILE_LIST "${CMAKE_CURRENT_LIST_DIR}/copyright")
