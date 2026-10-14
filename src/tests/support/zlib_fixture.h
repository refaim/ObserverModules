#ifndef UNPACKRAT_TESTS_SUPPORT_ZLIB_FIXTURE_H
#define UNPACKRAT_TESTS_SUPPORT_ZLIB_FIXTURE_H

#include <cstddef>
#include <span>
#include <vector>

namespace test::support
{
    [[nodiscard]] std::vector<std::byte> compress_zlib_fixture(std::span<const std::byte> input);
}
#endif
