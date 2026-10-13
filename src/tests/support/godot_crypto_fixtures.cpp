#include "godot_crypto_fixtures.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

#include <mbedtls/aes.h>
#include <mbedtls/md5.h>

namespace test::support
{
    unpackrat::codecs::key256 godot_test_key()
    {
        unpackrat::codecs::key256 key{};
        for (std::size_t index = 0; index < key.size(); ++index) {
            key[index] = static_cast<std::byte>(index + 1);
        }
        return key;
    }

    godot_bytes encrypted_envelope(const std::span<const std::uint8_t> plaintext, const unpackrat::codecs::key256 &key)
    {
        std::array<unsigned char, 16> digest{};
        if (mbedtls_md5(plaintext.data(), plaintext.size(), digest.data()) != 0) {
            throw std::runtime_error("fixture MD5 failed");
        }
        std::array<unsigned char, 16> iv{};
        for (std::size_t index = 0; index < iv.size(); ++index) {
            iv[index] = static_cast<unsigned char>(0xa0 + index);
        }
        godot_bytes padded(plaintext.begin(), plaintext.end());
        padded.resize((padded.size() + 15) & ~std::size_t{15}, 0);
        godot_bytes ciphertext(padded.size());
        mbedtls_aes_context context;
        mbedtls_aes_init(&context);
        static_cast<void>(mbedtls_aes_setkey_enc(&context, reinterpret_cast<const unsigned char *>(key.data()), 256));
        auto state_iv = iv;
        std::size_t offset = 0;
        const int result = mbedtls_aes_crypt_cfb128(&context, MBEDTLS_AES_ENCRYPT, padded.size(), &offset,
                                                    state_iv.data(), padded.data(), ciphertext.data());
        mbedtls_aes_free(&context);
        if (result != 0) {
            throw std::runtime_error("fixture AES failed");
        }
        godot_bytes output(digest.begin(), digest.end());
        const auto length = static_cast<std::uint64_t>(plaintext.size());
        for (unsigned shift = 0; shift < 64; shift += 8) {
            output.push_back(static_cast<std::uint8_t>(length >> shift));
        }
        output.insert(output.end(), iv.begin(), iv.end());
        output.insert(output.end(), ciphertext.begin(), ciphertext.end());
        return output;
    }

    godot_bytes encrypted_pck(const std::uint32_t version, const bool directory, const bool member,
                              const unpackrat::codecs::key256 &key)
    {
        godot_pck_options options;
        options.version = version;
        options.pack_flags = directory ? 1U : 0U;
        godot_entry_options entry;
        entry.path = "res://data.bin";
        entry.payload = {'h', 'e', 'l', 'l', 'o'};
        if (member) {
            entry.stored_size = entry.payload.size();
            entry.payload = encrypted_envelope(entry.payload, key);
            entry.flags = 1;
        }
        options.entries.push_back(entry);
        godot_bytes image = build_godot_pck(options);
        if (!directory) {
            return image;
        }
        const std::size_t header_bytes = version == 2 ? 96 : 40;
        constexpr std::size_t stored_name_bytes = 16;
        constexpr std::size_t directory_entry_bytes = 4 + stored_name_bytes + 8 + 8 + 16 + 4;
        const std::size_t directory_begin = header_bytes + 4;
        const std::size_t directory_end = directory_begin + directory_entry_bytes;
        const std::size_t encrypted_directory_bytes = 40 + ((directory_entry_bytes + 15) & ~std::size_t{15});
        patch_u64(image, directory_begin + 4 + stored_name_bytes,
                  static_cast<std::uint64_t>(directory_end) + encrypted_directory_bytes - directory_entry_bytes);
        const godot_bytes encrypted = encrypted_envelope(
            std::span<const std::uint8_t>(image).subspan(directory_begin, directory_entry_bytes), key);
        godot_bytes output(image.begin(), image.begin() + static_cast<std::ptrdiff_t>(directory_begin));
        output.insert(output.end(), encrypted.begin(), encrypted.end());
        output.insert(output.end(), image.begin() + static_cast<std::ptrdiff_t>(directory_end), image.end());
        return output;
    }
} // namespace test::support
