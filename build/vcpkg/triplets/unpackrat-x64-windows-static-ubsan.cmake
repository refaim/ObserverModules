# Static dependency libraries with the DLL CRT used by official LLVM UBSan.
include("${CMAKE_CURRENT_LIST_DIR}/unpackrat-x64-windows-static.cmake")
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_BUILD_TYPE release)
