#pragma once

#include <array>
#include <cstddef>
#include <memory>
#include <span>
#include <stdexcept>

namespace unpackrat::codecs
{
    using key256 = std::array<std::byte, 32>;
    using iv128 = std::array<std::byte, 16>;
    using digest128 = std::array<std::byte, 16>;

    class crypto_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    void erase_key(key256 &key) noexcept;

    // Owns the expanded AES key; mbedTLS erases its context on release. Input and output may
    // alias exactly. Arbitrary chunk boundaries preserve CFB128's byte offset and feedback IV.
    class aes256_cfb128 final
    {
      public:
        aes256_cfb128(const key256 &key, const iv128 &iv);
        ~aes256_cfb128();
        aes256_cfb128(const aes256_cfb128 &) = delete;
        aes256_cfb128 &operator=(const aes256_cfb128 &) = delete;

        void reset(const iv128 &iv) noexcept;
        void decrypt(std::span<const std::byte> input, std::span<std::byte> output);

      private:
        struct state;
        std::unique_ptr<state> state_;
    };

    // A single incremental MD5 operation. finish consumes the operation; no third-party
    // context, allocator, digest buffer or return code crosses this adapter boundary.
    class md5_state final
    {
      public:
        md5_state();
        ~md5_state();
        md5_state(const md5_state &) = delete;
        md5_state &operator=(const md5_state &) = delete;

        void update(std::span<const std::byte> input);
        [[nodiscard]] digest128 finish();

      private:
        struct state;
        std::unique_ptr<state> state_;
    };
} // namespace unpackrat::codecs
