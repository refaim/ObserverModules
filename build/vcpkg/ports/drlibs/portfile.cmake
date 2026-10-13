# Keep both adapters on the exact tested dr_libs revision.
vcpkg_download_distfile(DR_MP3_HEADER
    URLS "https://raw.githubusercontent.com/mackron/dr_libs/dfe8377631000664666519fdb83da193fd8037f4/dr_mp3.h"
    FILENAME "dr_libs-dfe8377631000664666519fdb83da193fd8037f4-dr_mp3.h"
    SHA512 c21d523bdd890bf8b2e84ff7ac63f72299b235834b424d95d281b57c4e22d260e23e423ed8d829e710cbd11d8814c69afef24cb9e576bbb9cb0b704be94bf8ed
)
vcpkg_download_distfile(DR_WAV_HEADER
    URLS "https://raw.githubusercontent.com/mackron/dr_libs/dfe8377631000664666519fdb83da193fd8037f4/dr_wav.h"
    FILENAME "dr_libs-dfe8377631000664666519fdb83da193fd8037f4-dr_wav.h"
    SHA512 277ada6b7cf940f557d38dea7985db129c8aaf034c23425da3f1681e901439bbd4152a766de5a26cc7ddb3f0a00f2ce8f6496f61c359bc65889eeac25e356bd3
)

set(SOURCE_PATH "${CURRENT_BUILDTREES_DIR}/source")
file(MAKE_DIRECTORY "${SOURCE_PATH}")
file(COPY_FILE "${DR_MP3_HEADER}" "${SOURCE_PATH}/dr_mp3.h")
file(COPY_FILE "${DR_WAV_HEADER}" "${SOURCE_PATH}/dr_wav.h")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/dr_mp3.c" "${SOURCE_PATH}/dr_mp3.c")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/dr_wav.c" "${SOURCE_PATH}/dr_wav.c")
file(COPY_FILE "${CMAKE_CURRENT_LIST_DIR}/CMakeLists.txt" "${SOURCE_PATH}/CMakeLists.txt")

vcpkg_cmake_configure(SOURCE_PATH "${SOURCE_PATH}")
vcpkg_cmake_install()
vcpkg_copy_pdbs()
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/include" "${CURRENT_PACKAGES_DIR}/debug/share")
vcpkg_install_copyright(FILE_LIST "${CMAKE_CURRENT_LIST_DIR}/copyright")
