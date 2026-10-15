#include "crypto_codec.h"

#include <algorithm>
#include <cstddef>
#include <memory>
#include <mutex>

#include <mbedtls/platform_util.h>
#include <psa/crypto.h>
#include <psa/crypto_extra.h>

namespace unpackrat::codecs
{
    namespace
    {
        // PSA owns a module-wide key store and RNG. The port deliberately leaves its
        // threading layer disabled, so every PSA operation, including cleanup, uses
        // this mutex. Archive contexts are closed before Observer unloads the module;
        // the runtime then releases the provider's global resources exactly once.
        struct provider_runtime final
        {
            std::mutex mutex;

            ~provider_runtime()
            {
                const std::lock_guard lock(mutex);
                mbedtls_psa_crypto_free();
            }
        };

        provider_runtime &provider()
        {
            static provider_runtime runtime;
            return runtime;
        }

        void checked_status(const psa_status_t status)
        {
            if (status != PSA_SUCCESS) {
                throw crypto_error("PSA cryptographic operation failed");
            }
        }

        // The default PSA configuration copies its input/output buffers, which also
        // permits exact in-place calls. Bound each request so those library-owned
        // temporary copies never grow with an archive member's untrusted length.
        constexpr std::size_t crypto_chunk_bytes = 4096;

        struct key_attributes final
        {
            key_attributes() = default;
            ~key_attributes()
            {
                // This value-only cleanup runs inside volatile_key's runtime guard.
                psa_reset_key_attributes(&value);
            }
            key_attributes(const key_attributes &) = delete;
            key_attributes &operator=(const key_attributes &) = delete;

            psa_key_attributes_t value = PSA_KEY_ATTRIBUTES_INIT;
        };

        struct volatile_key final
        {
            explicit volatile_key(const key256 &key)
            {
                const std::lock_guard lock(provider().mutex);
                checked_status(psa_crypto_init());
                key_attributes attributes;
                psa_set_key_lifetime(&attributes.value, PSA_KEY_LIFETIME_VOLATILE);
                psa_set_key_type(&attributes.value, PSA_KEY_TYPE_AES);
                psa_set_key_bits(&attributes.value, 256);
                psa_set_key_usage_flags(&attributes.value, PSA_KEY_USAGE_DECRYPT);
                psa_set_key_algorithm(&attributes.value, PSA_ALG_CFB);
                // A failed import leaves the output handle zero and releases its
                // partial allocation. No fallible work follows a successful import.
                checked_status(psa_import_key(&attributes.value, reinterpret_cast<const unsigned char *>(key.data()),
                                              key.size(), &value));
            }

            ~volatile_key()
            {
                const std::lock_guard lock(provider().mutex);
                // The pinned software provider accepts zero and valid volatile
                // handles here; no persistent storage or external driver is used.
                static_cast<void>(psa_destroy_key(value));
            }

            volatile_key(const volatile_key &) = delete;
            volatile_key &operator=(const volatile_key &) = delete;
            mbedtls_svc_key_id_t value = 0;
        };

        struct cipher_operation final
        {
            cipher_operation() = default;
            ~cipher_operation()
            {
                const std::lock_guard lock(provider().mutex);
                // Aborting a zero, active or already-aborted software operation
                // succeeds and releases its context without allocating.
                static_cast<void>(psa_cipher_abort(&value));
            }
            cipher_operation(const cipher_operation &) = delete;
            cipher_operation &operator=(const cipher_operation &) = delete;
            psa_cipher_operation_t value = PSA_CIPHER_OPERATION_INIT;
        };

        struct hash_operation final
        {
            hash_operation() = default;
            ~hash_operation()
            {
                const std::lock_guard lock(provider().mutex);
                // Finished and failed hash operations are safe to abort as well.
                static_cast<void>(psa_hash_abort(&value));
            }
            hash_operation(const hash_operation &) = delete;
            hash_operation &operator=(const hash_operation &) = delete;
            psa_hash_operation_t value = PSA_HASH_OPERATION_INIT;
        };
    } // namespace

    struct aes256_cfb128::state final
    {
        state(const key256 &key_value, const iv128 &iv_value) : key(key_value), iv(iv_value)
        {
        }

        ~state()
        {
            mbedtls_platform_zeroize(iv.data(), iv.size());
        }

        state(const state &) = delete;
        state &operator=(const state &) = delete;

        volatile_key key;
        cipher_operation operation;
        iv128 iv{};
        bool pending_reset = true;
    };

    aes256_cfb128::aes256_cfb128(const key256 &key, const iv128 &iv) : state_(std::make_unique<state>(key, iv))
    {
    }

    aes256_cfb128::~aes256_cfb128() = default;

    void aes256_cfb128::reset(const iv128 &iv) noexcept
    {
        state_->iv = iv;
        // Deferring the fallible PSA setup keeps random-access reset noexcept.
        state_->pending_reset = true;
    }

    void aes256_cfb128::decrypt(const std::span<const std::byte> input, const std::span<std::byte> output)
    {
        if (input.size() != output.size()) {
            throw crypto_error("AES-CFB input and output lengths differ");
        }
        const std::lock_guard lock(provider().mutex);
        if (state_->pending_reset) {
            static_cast<void>(psa_cipher_abort(&state_->operation.value));
            checked_status(psa_cipher_decrypt_setup(&state_->operation.value, state_->key.value, PSA_ALG_CFB));
            checked_status(psa_cipher_set_iv(&state_->operation.value,
                                             reinterpret_cast<const unsigned char *>(state_->iv.data()),
                                             state_->iv.size()));
            state_->pending_reset = false;
        }
        std::size_t offset = 0;
        while (offset < input.size()) {
            const auto count = (std::min)(crypto_chunk_bytes, input.size() - offset);
            std::size_t written = 0;
            checked_status(psa_cipher_update(
                &state_->operation.value, reinterpret_cast<const unsigned char *>(input.data() + offset), count,
                reinterpret_cast<unsigned char *>(output.data() + offset), count, &written));
            // The pinned built-in CFB driver emits exactly count bytes per update,
            // retaining its feedback offset across requests; it has no final block.
            offset += count;
        }
    }

    void erase_key(key256 &key) noexcept
    {
        mbedtls_platform_zeroize(key.data(), key.size());
    }

    struct md5_state::state final
    {
        state()
        {
            const std::lock_guard lock(provider().mutex);
            checked_status(psa_crypto_init());
            checked_status(psa_hash_setup(&operation.value, PSA_ALG_MD5));
        }

        state(const state &) = delete;
        state &operator=(const state &) = delete;
        hash_operation operation;
    };

    md5_state::md5_state() : state_(std::make_unique<state>())
    {
    }
    md5_state::~md5_state() = default;

    void md5_state::update(const std::span<const std::byte> input)
    {
        const std::lock_guard lock(provider().mutex);
        auto remaining = input;
        do {
            const auto chunk = remaining.first((std::min)(crypto_chunk_bytes, remaining.size()));
            // Even an empty update must validate that finish or an earlier failure
            // has not consumed the operation. PSA does not copy a zero-length input.
            checked_status(psa_hash_update(&state_->operation.value,
                                           reinterpret_cast<const unsigned char *>(chunk.data()), chunk.size()));
            remaining = remaining.subspan(chunk.size());
        } while (!remaining.empty());
    }

    digest128 md5_state::finish()
    {
        digest128 digest{};
        const std::lock_guard lock(provider().mutex);
        std::size_t written = 0;
        checked_status(psa_hash_finish(&state_->operation.value, reinterpret_cast<unsigned char *>(digest.data()),
                                       digest.size(), &written));
        // PSA_ALG_MD5 has a fixed 16-byte output, exactly the caller-owned digest.
        return digest;
    }
} // namespace unpackrat::codecs
