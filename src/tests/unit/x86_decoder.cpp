#include "../../core/codecs/x86_decoder.h"
#include <array>
#include <catch2/catch_test_macros.hpp>
#include <initializer_list>
#include <vector>
namespace
{
    using namespace unpackrat::codecs;
    std::optional<x86_instruction> decode(std::initializer_list<unsigned char> bytes, x86_mode mode = x86_mode::x64)
    {
        std::vector<std::byte> code;
        for (auto value : bytes)
            code.push_back(static_cast<std::byte>(value));
        return decode_x86(code, 0x140001000, mode);
    }
    x86_instruction checked(std::initializer_list<unsigned char> bytes, x86_mode mode = x86_mode::x64)
    {
        const auto result = decode(bytes, mode);
        REQUIRE(result.has_value());
        return result.value_or(x86_instruction{});
    }
} // namespace
TEST_CASE("x86 decoder: resolves only static references", "[x86-decoder]")
{
    const auto rip = checked({0x48, 0x8d, 0x0d, 0x34, 0x12, 0, 0});
    CHECK(rip.length == 7);
    REQUIRE(rip.address_count == 1);
    CHECK(rip.addresses[0] == 0x14000223b);
    const auto absolute = checked({0xa1, 0x00, 0x20, 0x40, 0x00}, x86_mode::x86);
    REQUIRE(absolute.address_count == 1);
    CHECK(absolute.addresses[0] == 0x402000);
    const auto immediate = checked({0xb9, 0x00, 0x20, 0x40, 0x00}, x86_mode::x86);
    CHECK(immediate.addresses[0] == 0x402000);
    CHECK(checked({0x48, 0x8b, 0x43, 0x10}).address_count == 0);
    const auto indexed = checked({0x48, 0x8b, 0x04, 0x85, 0x00, 0x20, 0x40, 0x00});
    CHECK(indexed.address_count == 0);
    CHECK(indexed.indexed_displacement == 0x402000);
    const auto eip = checked({0x67, 0x48, 0x8d, 0x0d, 0x34, 0x12, 0, 0});
    CHECK(eip.addresses[0] == 0x4000223c);
}
TEST_CASE("x86 decoder: exposes bounded pattern facts without executing instructions", "[x86-decoder]")
{
    CHECK(checked({0xba, 0x20, 0, 0, 0}).size32);
    CHECK(checked({0x3d, 0x47, 0x44, 0x50, 0x43}).pack_magic);
    CHECK(checked({0x31, 0xc0}).bit_xor);
    CHECK(checked({0x0f, 0x57, 0xc1}).bit_xor);
    CHECK(checked({0x66, 0x0f, 0xef, 0xc1}).bit_xor);
    CHECK(checked({0xc3}).returns);
    const auto call = checked({0xe8, 0x10, 0, 0, 0});
    CHECK(call.call_target == 0x140001015);
    CHECK(!checked({0xff, 0xd0}).call_target.has_value());
    CHECK(!checked({0xeb, 0x10}).call_target.has_value());
    CHECK(!decode({}).has_value());
    CHECK(!decode({0x0f}).has_value());
}
