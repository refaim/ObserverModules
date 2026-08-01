#include "../../core/compression/zlib_codec.h"
#include "../support/zlib_fixture.h"

#include <array>
#include <cstdint>
#include <vector>

#ifdef _DEBUG
#include <crtdbg.h>
#endif

#include <catch2/catch_test_macros.hpp>

namespace
{
#ifdef _DEBUG
    thread_local bool reject_next_allocation = false;

    int __cdecl allocation_hook(const int allocation_type, void *, const std::size_t, const int, const long,
                                const unsigned char *, const int)
    {
        if (allocation_type == _HOOK_ALLOC && reject_next_allocation) {
            reject_next_allocation = false;
            return 0;
        }
        return 1;
    }

    class scoped_allocation_failure final
    {
      public:
        scoped_allocation_failure() : previous_(_CrtSetAllocHook(allocation_hook))
        {
            reject_next_allocation = true;
        }

        ~scoped_allocation_failure()
        {
            reject_next_allocation = false;
            static_cast<void>(_CrtSetAllocHook(previous_));
        }

        scoped_allocation_failure(const scoped_allocation_failure &) = delete;
        scoped_allocation_failure &operator=(const scoped_allocation_failure &) = delete;

      private:
        _CRT_ALLOC_HOOK previous_ = nullptr;
    };

    void decompress_with_failed_initial_allocation(const std::span<const std::byte> input)
    {
        scoped_allocation_failure failure;
        static_cast<void>(observer::compression::decompress_zlib(input, 1));
    }
#endif
} // namespace

TEST_CASE("compression: zlib round trip and output budget")
{
    constexpr std::array input{
        std::byte{0x6f}, std::byte{0x62}, std::byte{0x73}, std::byte{0x65},
        std::byte{0x72}, std::byte{0x76}, std::byte{0x65}, std::byte{0x72},
    };
    const auto compressed = test::support::compress_zlib_fixture(input);
    REQUIRE(observer::compression::decompress_zlib(compressed, input.size()) ==
            std::vector<std::byte>(input.begin(), input.end()));
    REQUIRE_THROWS_AS(observer::compression::decompress_zlib(compressed, input.size() - 1),
                      observer::compression::error);

    auto corrupted = compressed;
    corrupted.back() ^= std::byte{0xff};
    REQUIRE_THROWS_AS(observer::compression::decompress_zlib(corrupted, input.size()), observer::compression::error);
    REQUIRE_THROWS_AS(observer::compression::decompress_zlib({}, input.size()), observer::compression::error);
}

TEST_CASE("compression: zlib consumes multiple output chunks and rejects trailing or truncated input")
{
    std::vector<std::byte> input(std::size_t{192} * 1024);
    std::uint32_t state = 0x9e3779b9;
    for (auto &byte : input) {
        state = state * 1664525 + 1013904223;
        byte = static_cast<std::byte>(state >> 24);
    }

    const auto compressed = test::support::compress_zlib_fixture(input);
    REQUIRE(observer::compression::decompress_zlib(compressed, input.size()) == input);

    auto trailing = compressed;
    trailing.push_back(std::byte{0});
    REQUIRE_THROWS_AS(observer::compression::decompress_zlib(trailing, input.size()), observer::compression::error);

    auto truncated = compressed;
    truncated.pop_back();
    REQUIRE_THROWS_AS(observer::compression::decompress_zlib(truncated, input.size()), observer::compression::error);
}

TEST_CASE("compression: zlib supports an empty payload")
{
    const auto compressed = test::support::compress_zlib_fixture({});
    REQUIRE(observer::compression::decompress_zlib(compressed, 0).empty());
}

TEST_CASE("compression: zlib normalizes a stream that requires a preset dictionary")
{
    constexpr std::array compressed_with_dictionary{
        std::byte{0x78}, std::byte{0xbb}, std::byte{0}, std::byte{0}, std::byte{0}, std::byte{1},
    };
    REQUIRE_THROWS_AS(observer::compression::decompress_zlib(compressed_with_dictionary, 0),
                      observer::compression::error);
}

#ifdef _DEBUG
TEST_CASE("compression: zlib initialization failure is normalized")
{
    const auto compressed = test::support::compress_zlib_fixture(std::array{std::byte{1}});
    REQUIRE_THROWS_AS(decompress_with_failed_initial_allocation(compressed), observer::compression::error);
}
#endif
