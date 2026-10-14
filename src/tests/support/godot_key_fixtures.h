#ifndef UNPACKRAT_TESTS_SUPPORT_GODOT_KEY_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_GODOT_KEY_FIXTURES_H
#include "../../core/codecs/crypto_codec.h"
#include <vector>
namespace test::support
{
    enum class key_shape : std::uint8_t
    {
        direct_scalar,
        pointer_scalar,
        direct_simd,
        pointer_simd,
        xor3_scalar,
        xor3_simd
    };
    struct key_fixture final
    {
        std::vector<std::byte> image;
        unpackrat::codecs::key256 expected{};
    };
    [[nodiscard]] key_fixture make_key_fixture(key_shape shape = key_shape::direct_scalar, bool x64 = true);
} // namespace test::support
#endif
