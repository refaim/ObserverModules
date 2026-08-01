#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd -- "${script_directory}/../.." && pwd -P)"
artifact_root="${repository_root}/.artifacts"
object_directory="${artifact_root}/mutation/obj"
report_directory="${artifact_root}/reports/mutation"
test_binary="${artifact_root}/mutation/pickle-tests"

: "${MULL_LLVM:?MULL_LLVM must select the installed Clang and Mull major version}"
: "${VCPKG_INSTALLED_DIR:?VCPKG_INSTALLED_DIR must point to the pinned vcpkg install root}"

compiler="clang++-${MULL_LLVM}"
mull_runner="mull-runner-${MULL_LLVM}"
mull_plugin="/usr/lib/mull-ir-frontend-${MULL_LLVM}"
catch_include="${VCPKG_INSTALLED_DIR}/x64-linux/include"
catch_library="${VCPKG_INSTALLED_DIR}/x64-linux/lib"
report_path="${report_directory}/pickle.json"

for executable in "${compiler}" "${mull_runner}" python3; do
    command -v "${executable}" >/dev/null
done
for required_file in \
    "${mull_plugin}" \
    "${catch_include}/catch2/catch_session.hpp" \
    "${catch_library}/libCatch2.a"; do
    test -f "${required_file}"
done

mkdir -p -- "${object_directory}" "${report_directory}"

common_flags=(
    -std=c++23
    -O0
    -g
    -fno-omit-frame-pointer
    -Wall
    -Wextra
    -Wpedantic
    -Werror
    -isystem "${catch_include}"
)

export MULL_CONFIG="${repository_root}/build/mutation/mull.yml"

"${compiler}" "${common_flags[@]}" \
    "-fpass-plugin=/usr/lib/mull-ir-frontend-${MULL_LLVM}" \
    -c "${repository_root}/src/modules/renpy/pickle.cpp" \
    -o "${object_directory}/pickle.o"
"${compiler}" "${common_flags[@]}" \
    -c "${repository_root}/src/tests/unit/pickle.cpp" \
    -o "${object_directory}/pickle-tests.o"
"${compiler}" "${common_flags[@]}" \
    -c "${repository_root}/src/tests/mutation/main.cpp" \
    -o "${object_directory}/main.o"
"${compiler}" \
    "${object_directory}/pickle.o" \
    "${object_directory}/pickle-tests.o" \
    "${object_directory}/main.o" \
    -L "${catch_library}" \
    -lCatch2 \
    -pthread \
    -o "${test_binary}"

"${test_binary}"

"${mull_runner}" \
    --workers 2 \
    --strict \
    --mutation-score-threshold 100 \
    --no-output \
    --reporters Elements \
    --report-dir "${report_directory}" \
    --report-name pickle \
    "${test_binary}" \
    2>&1 | tee "${report_directory}/mull.log"

python3 "${repository_root}/build/mutation/validate_report.py" "${report_path}"
