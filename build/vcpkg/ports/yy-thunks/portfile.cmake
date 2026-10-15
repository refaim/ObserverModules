if(NOT VCPKG_TARGET_ARCHITECTURE MATCHES "^(x86|x64)$")
    message(FATAL_ERROR "YY-Thunks supports only x86 and x64")
endif()

# LibMaker is an explicitly installed prerequisite; never acquire a tool here.
find_program(YY_THUNKS_LIBMAKER NAMES LibMaker.exe PATHS ENV PATH NO_DEFAULT_PATH REQUIRED)

vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO Chuyu-Team/YY-Thunks
    REF b41fb80b8d316581eec0959600aeb0e7339d0e5e
    SHA512 b949f296a412dfbf901060d4903d3b1dcc1b9afdad61b7eefbee7b86b77e58690fd17827e75b2337a3535fc6fe306976d6714058be1d427963c3358c29d273aa
    PATCHES ordinal-fallback.patch hardened-build.patch
)

set(YY_THUNKS_OBJECT "${SOURCE_PATH}/objs/${VCPKG_TARGET_ARCHITECTURE}/YY_Thunks_for_WinXP.obj")
# A successful command must produce this invocation's object, not reuse an old one.
file(REMOVE "${YY_THUNKS_OBJECT}")
set(ENV{YY_THUNKS_HARDENED} 1)
set(ENV{YY_THUNKS_OBJECT_TARGET} WinXP)
set(ENV{Platform} "${VCPKG_TARGET_ARCHITECTURE}")
vcpkg_execute_required_process(
    COMMAND cmd /d /c Build.cmd
    WORKING_DIRECTORY "${SOURCE_PATH}/src"
    LOGNAME "build-${TARGET_TRIPLET}"
)
if(NOT EXISTS "${YY_THUNKS_OBJECT}")
    message(FATAL_ERROR "YY-Thunks build did not produce ${YY_THUNKS_OBJECT}")
endif()
file(INSTALL "${YY_THUNKS_OBJECT}"
    DESTINATION "${CURRENT_PACKAGES_DIR}/share/${PORT}/objs/${VCPKG_TARGET_ARCHITECTURE}")
vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/LICENSE")
set(VCPKG_POLICY_EMPTY_INCLUDE_FOLDER enabled)
