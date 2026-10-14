#ifndef UNPACKRAT_TESTS_SUPPORT_GODOT_CRYPTO_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_GODOT_CRYPTO_FIXTURES_H

#include "../../core/codecs/crypto_codec.h"
#include "godot_fixtures.h"

#include <cstddef>
#include <cstdint>
#include <span>

namespace test::support
{
    [[nodiscard]] unpackrat::codecs::key256 godot_test_key();
    [[nodiscard]] godot_bytes encrypted_envelope(std::span<const std::uint8_t> plaintext,
                                                 const unpackrat::codecs::key256 &key = godot_test_key());
    [[nodiscard]] godot_bytes encrypted_pck(std::uint32_t version, bool directory, bool member,
                                            const unpackrat::codecs::key256 &key = godot_test_key());
} // namespace test::support
#endif
