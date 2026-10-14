#ifndef UNPACKRAT_CORE_GODOT_ENCRYPTED_PAYLOAD_H
#define UNPACKRAT_CORE_GODOT_ENCRYPTED_PAYLOAD_H

#include "../codecs/crypto_codec.h"
#include "pck.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <istream>
#include <limits>
#include <memory>
#include <stdexcept>

namespace unpackrat::godot
{
    inline constexpr std::size_t encryption_chunk_bytes = std::size_t{64} * 1024;

    class encryption_error : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // MD5 mismatch cannot distinguish a wrong key from damaged ciphertext. Candidate discovery
    // may try another bounded candidate; it must never accept a merely plausible prefix.
    class key_mismatch final : public encryption_error
    {
      public:
        using encryption_error::encryption_error;
    };

    struct encryption_limits final
    {
        std::uint64_t max_plaintext_bytes = (std::numeric_limits<std::uint64_t>::max)();
    };

    struct encrypted_payload final
    {
        byte_range envelope{};
        byte_range ciphertext{};
        std::uint64_t logical_size = 0;
        codecs::iv128 iv{};
        codecs::digest128 md5{};

        [[nodiscard]] bool key_evidence() const noexcept;
    };

    // Godot PCK envelopes omit GDEC magic: MD5[16], length u64 LE, IV[16], padded ciphertext.
    // source is an enclosing physical window; the returned envelope is the exact stored extent.
    [[nodiscard]] encrypted_payload read_encrypted_payload(std::istream &stream, byte_range source,
                                                           const encryption_limits &limits = {});

    // A bounded seekable plaintext view, borrowing the physical stream. AES owns its expanded
    // key and erases it on destruction. Random access restarts CFB at a block boundary using the
    // preceding ciphertext block as IV. No whole-member allocation is performed.
    class verified_payload_source final
    {
      public:
        verified_payload_source(std::istream &stream, const encrypted_payload &payload, const codecs::key256 &key);
        ~verified_payload_source();
        verified_payload_source(const verified_payload_source &) = delete;
        verified_payload_source &operator=(const verified_payload_source &) = delete;

        void verify(const std::function<void(std::uint64_t)> &progress = {});
        [[nodiscard]] bool verified() const noexcept;
        [[nodiscard]] std::istream &stream();
        // For bounded structural probes only, never candidate-key validation or committed output.
        [[nodiscard]] std::istream &unverified_stream() noexcept;

      private:
        struct state;
        std::unique_ptr<state> state_;
        std::istream view_;
    };
} // namespace unpackrat::godot
#endif
