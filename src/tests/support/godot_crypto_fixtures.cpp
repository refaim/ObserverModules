#include "godot_crypto_fixtures.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

#include <psa/crypto.h>

namespace
{
    void fixture_status(const psa_status_t status)
    {
        if (status != PSA_SUCCESS) {
            throw std::runtime_error("fixture PSA operation failed");
        }
    }

    struct fixture_cipher final
    {
        fixture_cipher() = default;
        ~fixture_cipher()
        {
            static_cast<void>(psa_cipher_abort(&operation));
            static_cast<void>(psa_destroy_key(key));
            psa_reset_key_attributes(&attributes);
        }
        fixture_cipher(const fixture_cipher &) = delete;
        fixture_cipher &operator=(const fixture_cipher &) = delete;

        psa_key_attributes_t attributes = PSA_KEY_ATTRIBUTES_INIT;
        mbedtls_svc_key_id_t key = 0;
        psa_cipher_operation_t operation = PSA_CIPHER_OPERATION_INIT;
    };
} // namespace

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
        // Fixture construction is sequential. Concurrent adapter tests create their
        // independent NIST vectors before starting any worker threads.
        fixture_status(psa_crypto_init());
        std::array<unsigned char, 16> digest{};
        std::size_t digest_bytes = 0;
        fixture_status(psa_hash_compute(PSA_ALG_MD5, plaintext.data(), plaintext.size(), digest.data(), digest.size(),
                                        &digest_bytes));
        std::array<unsigned char, 16> iv{};
        for (std::size_t index = 0; index < iv.size(); ++index) {
            iv[index] = static_cast<unsigned char>(0xa0 + index);
        }
        godot_bytes padded(plaintext.begin(), plaintext.end());
        padded.resize((padded.size() + 15) & ~std::size_t{15}, 0);
        godot_bytes ciphertext(padded.size());
        fixture_cipher cipher;
        psa_set_key_type(&cipher.attributes, PSA_KEY_TYPE_AES);
        psa_set_key_bits(&cipher.attributes, 256);
        psa_set_key_usage_flags(&cipher.attributes, PSA_KEY_USAGE_ENCRYPT);
        psa_set_key_algorithm(&cipher.attributes, PSA_ALG_CFB);
        fixture_status(psa_import_key(&cipher.attributes, reinterpret_cast<const unsigned char *>(key.data()),
                                      key.size(), &cipher.key));
        fixture_status(psa_cipher_encrypt_setup(&cipher.operation, cipher.key, PSA_ALG_CFB));
        fixture_status(psa_cipher_set_iv(&cipher.operation, iv.data(), iv.size()));
        std::size_t written = 0;
        fixture_status(psa_cipher_update(&cipher.operation, padded.data(), padded.size(), ciphertext.data(),
                                         ciphertext.size(), &written));
        std::array<std::uint8_t, PSA_CIPHER_FINISH_OUTPUT_SIZE(PSA_KEY_TYPE_AES, PSA_ALG_CFB)> tail{};
        std::size_t tail_bytes = 0;
        fixture_status(psa_cipher_finish(&cipher.operation, tail.data(), tail.size(), &tail_bytes));
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
