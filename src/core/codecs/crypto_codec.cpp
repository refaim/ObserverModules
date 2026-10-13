#include "crypto_codec.h"

#include <array>
#include <cstddef>
#include <memory>

#include <mbedtls/aes.h>
#include <mbedtls/md5.h>
#include <mbedtls/platform_util.h>

namespace unpackrat::codecs
{
    struct aes256_cfb128::state final
    {
        explicit state(const key256 &key)
        {
            mbedtls_aes_init(&context);
            // The portable mbedTLS AES implementation accepts exactly these fixed key bits;
            // invalid-key-length is its only setkey failure and cannot occur at this boundary.
            static_cast<void>(
                mbedtls_aes_setkey_enc(&context, reinterpret_cast<const unsigned char *>(key.data()), 256));
        }

        ~state()
        {
            mbedtls_aes_free(&context);
            mbedtls_platform_zeroize(iv.data(), iv.size());
        }

        state(const state &) = delete;
        state &operator=(const state &) = delete;

        mbedtls_aes_context context{};
        std::array<unsigned char, 16> iv{};
        std::size_t offset = 0;
    };

    aes256_cfb128::aes256_cfb128(const key256 &key, const iv128 &iv) : state_(std::make_unique<state>(key))
    {
        reset(iv);
    }

    aes256_cfb128::~aes256_cfb128() = default;

    void aes256_cfb128::reset(const iv128 &iv) noexcept
    {
        for (std::size_t index = 0; index < iv.size(); ++index) {
            state_->iv[index] = std::to_integer<unsigned char>(iv[index]);
        }
        state_->offset = 0;
    }

    void aes256_cfb128::decrypt(const std::span<const std::byte> input, const std::span<std::byte> output)
    {
        if (input.size() != output.size()) {
            throw crypto_error("AES-CFB input and output lengths differ");
        }
        // Valid expanded context, mode and feedback offset make this call infallible in the
        // pinned portable implementation; it supports exact in-place decryption.
        static_cast<void>(mbedtls_aes_crypt_cfb128(
            &state_->context, MBEDTLS_AES_DECRYPT, input.size(), &state_->offset, state_->iv.data(),
            reinterpret_cast<const unsigned char *>(input.data()), reinterpret_cast<unsigned char *>(output.data())));
    }

    void erase_key(key256 &key) noexcept
    {
        mbedtls_platform_zeroize(key.data(), key.size());
    }

    struct md5_state::state final
    {
        state()
        {
            mbedtls_md5_init(&context);
            static_cast<void>(mbedtls_md5_starts(&context));
        }

        ~state()
        {
            mbedtls_md5_free(&context);
        }

        state(const state &) = delete;
        state &operator=(const state &) = delete;
        mbedtls_md5_context context{};
    };

    md5_state::md5_state() : state_(std::make_unique<state>())
    {
    }
    md5_state::~md5_state() = default;

    void md5_state::update(const std::span<const std::byte> input)
    {
        // The portable MD5 implementation returns zero for every initialized context and
        // bounded byte span. Hardware/alternate implementations are not enabled in this port.
        static_cast<void>(
            mbedtls_md5_update(&state_->context, reinterpret_cast<const unsigned char *>(input.data()), input.size()));
    }

    digest128 md5_state::finish()
    {
        digest128 digest{};
        static_cast<void>(mbedtls_md5_finish(&state_->context, reinterpret_cast<unsigned char *>(digest.data())));
        return digest;
    }
} // namespace unpackrat::codecs
