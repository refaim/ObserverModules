#include "core/io/byte_source.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <stdexcept>
#include <vector>

#include <catch2/catch_test_macros.hpp>

TEST_CASE("byte source: invalid ranges fail before callbacks and empty ranges are no-ops", "[byte-source]")
{
    struct range_case final
    {
        std::uint64_t size;
        std::uint64_t offset;
        std::uint64_t count;
        bool accepted;
    };
    constexpr auto maximum = (std::numeric_limits<std::uint64_t>::max)();
    constexpr range_case cases[] = {
        {10, 11, 0, false},     {10, 10, 1, false},           {10, 9, 2, false}, {10, 1, maximum, false},
        {0, maximum, 0, false}, {maximum, maximum, 1, false}, {10, 10, 0, true}, {10, 4, 0, true},
        {0, 0, 0, true},        {maximum, maximum, 0, true},
    };
    for (const auto &item : cases) {
        CAPTURE(item.size, item.offset, item.count);
        unsigned reads = 0;
        unsigned writes = 0;
        const unpackrat::io::byte_source source{item.size, [&](std::uint64_t, std::span<std::byte>) { ++reads; }};
        const unpackrat::io::byte_sink sink = [&](std::span<const std::byte>) { ++writes; };
        if (item.accepted)
            CHECK_NOTHROW(unpackrat::io::copy_byte_range(source, item.offset, item.count, sink));
        else
            CHECK_THROWS_AS(unpackrat::io::copy_byte_range(source, item.offset, item.count, sink), std::out_of_range);
        CHECK(reads == 0);
        CHECK(writes == 0);
    }
}

TEST_CASE("byte source: bounded chunks preserve an offset window byte for byte", "[byte-source]")
{
    std::vector<std::byte> stored(9003);
    for (std::size_t index = 0; index < stored.size(); ++index)
        stored[index] = static_cast<std::byte>(index % 251);
    std::vector<std::size_t> lengths;
    std::vector<std::byte> output;
    const unpackrat::io::byte_source source{
        stored.size(), [&](const std::uint64_t offset, const std::span<std::byte> target) {
            CHECK(offset == 3 + output.size());
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - static_cast<std::size_t>(offset));
            std::copy_n(stored.begin() + static_cast<std::ptrdiff_t>(offset), target.size(), target.begin());
            lengths.push_back(target.size());
        }};
    unpackrat::io::copy_byte_range(source, 3, 9000, [&](const std::span<const std::byte> bytes) {
        output.insert(output.end(), bytes.begin(), bytes.end());
    });
    CHECK(lengths == std::vector<std::size_t>{4096, 4096, 808});
    CHECK(output == std::vector<std::byte>(stored.begin() + 3, stored.end()));
}

TEST_CASE("byte source: callback errors stop copying and preserve the original exception", "[byte-source]")
{
    struct source_failure final : std::runtime_error
    {
        using std::runtime_error::runtime_error;
    };
    struct sink_failure final : std::runtime_error
    {
        using std::runtime_error::runtime_error;
    };
    unsigned reads = 0;
    std::vector<std::byte> output;
    SECTION("source failure after one chunk")
    {
        const unpackrat::io::byte_source source{9000,
                                                [&](const std::uint64_t offset, const std::span<std::byte> bytes) {
                                                    ++reads;
                                                    if (offset != 0)
                                                        throw source_failure("source stopped");
                                                    std::ranges::fill(bytes, std::byte{0x5a});
                                                }};
        CHECK_THROWS_AS(unpackrat::io::copy_byte_range(source, 0, 9000,
                                                       [&](const std::span<const std::byte> bytes) {
                                                           output.insert(output.end(), bytes.begin(), bytes.end());
                                                       }),
                        source_failure);
        CHECK(reads == 2);
        CHECK(output == std::vector<std::byte>(4096, std::byte{0x5a}));
    }
    SECTION("sink failure after its first read")
    {
        const unpackrat::io::byte_source source{9000, [&](std::uint64_t, std::span<std::byte>) { ++reads; }};
        const unpackrat::io::byte_sink sink = [](std::span<const std::byte>) { throw sink_failure("sink stopped"); };
        CHECK_THROWS_AS(unpackrat::io::copy_byte_range(source, 0, 9000, sink), sink_failure);
        CHECK(reads == 1);
    }
}
