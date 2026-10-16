# Standalone libFuzzer executables are never unloaded and keep the static CRT their
# supplied LLVM fuzzer archive is built with. Reuse the address-instrumented ASan recipe
# for the toolset pins and the same /fsanitize=address flags, then restore the static CRT.
include("${CMAKE_CURRENT_LIST_DIR}/unpackrat-x64-windows-static-asan.cmake")
set(VCPKG_CRT_LINKAGE static)
