#include "encrypted_payload.h"

#include "../io/bounded_stream.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <span>
#include <streambuf>
#include <vector>

namespace unpackrat::godot
{
    namespace
    {
        constexpr std::uint64_t envelope_header_bytes = 40;
        constexpr std::uint64_t cipher_block_bytes = 16;
    } // namespace

    bool encrypted_payload::key_evidence() const noexcept
    {
        return logical_size != 0;
    }

    encrypted_payload read_encrypted_payload(std::istream &stream, const byte_range source,
                                             const encryption_limits &limits)
    {
        try {
            io::bounded_stream input(stream);
            const auto file_size = static_cast<std::uint64_t>(input.size());
            if (source.offset > file_size || source.size > file_size - source.offset) {
                throw encryption_error("encrypted source range exceeds the stream");
            }
            if (source.size < envelope_header_bytes) {
                throw encryption_error("encrypted envelope header is truncated");
            }
            input.seek_absolute(static_cast<std::streamoff>(source.offset));
            std::array<std::byte, envelope_header_bytes> header{};
            input.read_exact(reinterpret_cast<char *>(header.data()), header.size());
            encrypted_payload payload;
            std::copy_n(header.begin(), payload.md5.size(), payload.md5.begin());
            std::copy_n(header.begin() + 24, payload.iv.size(), payload.iv.begin());
            for (unsigned index = 0; index < 8; ++index) {
                payload.logical_size |= static_cast<std::uint64_t>(std::to_integer<unsigned>(header[16 + index]))
                                        << (index * 8);
            }
            if (payload.logical_size > limits.max_plaintext_bytes ||
                payload.logical_size > (std::numeric_limits<std::uint64_t>::max)() - (cipher_block_bytes - 1)) {
                throw encryption_error("encrypted plaintext length exceeds its bound");
            }
            const std::uint64_t padded = (payload.logical_size + (cipher_block_bytes - 1)) & ~(cipher_block_bytes - 1);
            if (padded > source.size - envelope_header_bytes) {
                throw encryption_error("encrypted ciphertext is truncated");
            }
            payload.envelope = {source.offset, envelope_header_bytes + padded};
            payload.ciphertext = {source.offset + envelope_header_bytes, padded};
            return payload;
        } catch (const io::read_error &) {
            throw encryption_error("encrypted source is unreadable");
        }
    }

    struct verified_payload_source::state final : std::streambuf
    {
        state(std::istream &stream, const encrypted_payload &descriptor, const codecs::key256 &key)
            : payload(read_encrypted_payload(stream, descriptor.envelope)), input(stream), cipher(key, payload.iv),
              buffer(static_cast<std::size_t>(std::min<std::uint64_t>(payload.ciphertext.size, encryption_chunk_bytes)))
        {
        }

        void read_physical(const std::uint64_t offset, const std::span<std::byte> output)
        {
            try {
                input.seek_absolute(static_cast<std::streamoff>(offset));
                input.read_exact(reinterpret_cast<char *>(output.data()), output.size());
            } catch (const io::read_error &) {
                throw encryption_error("encrypted source is unreadable");
            }
        }

        [[nodiscard]] std::uint64_t position() const noexcept
        {
            return cursor + static_cast<std::uint64_t>(gptr() == nullptr ? 0 : gptr() - eback());
        }

        void rewind() noexcept
        {
            setg(nullptr, nullptr, nullptr);
            cursor = 0;
            cipher_cursor_valid = false;
        }

        void verify(const std::function<void(std::uint64_t)> &progress)
        {
            verified = false;
            rewind();
            cipher.reset(payload.iv);
            codecs::md5_state digest;
            std::uint64_t offset = 0;
            while (offset < payload.ciphertext.size) {
                const auto size =
                    static_cast<std::size_t>(std::min<std::uint64_t>(buffer.size(), payload.ciphertext.size - offset));
                const std::span<std::byte> chunk(buffer.data(), size);
                read_physical(payload.ciphertext.offset + offset, chunk);
                cipher.decrypt(chunk, chunk);
                const auto logical =
                    static_cast<std::size_t>(std::min<std::uint64_t>(size, payload.logical_size - offset));
                digest.update(chunk.first(logical));
                if (progress) {
                    progress(logical);
                }
                offset += size;
            }
            if (digest.finish() != payload.md5) {
                throw key_mismatch("encrypted plaintext MD5 mismatch");
            }
            verified = true;
            rewind();
        }

        void fill(const std::uint64_t wanted)
        {
            const std::uint64_t at = position();
            const bool sequential = cipher_cursor_valid && at == cipher_next;
            std::uint64_t start = at;
            if (!sequential) {
                cipher_cursor_valid = false;
                start = at & ~(cipher_block_bytes - 1);
                codecs::iv128 iv = payload.iv;
                if (start != 0) {
                    read_physical(payload.ciphertext.offset + start - cipher_block_bytes, iv);
                }
                cipher.reset(iv);
            }
            const auto count = static_cast<std::size_t>(std::min<std::uint64_t>(buffer.size(), wanted + at - start));
            const std::span<std::byte> chunk(buffer.data(), count);
            read_physical(payload.ciphertext.offset + start, chunk);
            cipher.decrypt(chunk, chunk);
            cipher_next = start + count;
            cipher_cursor_valid = true;
            cursor = start;
            auto *data = reinterpret_cast<char *>(buffer.data());
            setg(data, data + static_cast<std::ptrdiff_t>(at - start), data + count);
        }

        int_type underflow() override
        {
            if (position() == payload.logical_size) {
                return traits_type::eof();
            }
            fill(1);
            return traits_type::to_int_type(*gptr());
        }

        std::streamsize xsgetn(char_type *target, const std::streamsize count) override
        {
            if (count <= 0) {
                return 0;
            }
            const std::uint64_t wanted = std::min(static_cast<std::uint64_t>(count), payload.logical_size - position());
            std::uint64_t copied = 0;
            while (copied < wanted) {
                if (gptr() == nullptr || gptr() == egptr()) {
                    fill(wanted - copied);
                }
                const auto available = static_cast<std::size_t>(
                    std::min<std::uint64_t>(static_cast<std::uint64_t>(egptr() - gptr()), wanted - copied));
                std::memcpy(target + static_cast<std::size_t>(copied), gptr(), available);
                gbump(static_cast<int>(available)); // one bounded 64 KiB scratch chunk
                copied += available;
            }
            return static_cast<std::streamsize>(copied);
        }

        pos_type seekoff(const off_type offset, const std::ios_base::seekdir direction,
                         const std::ios_base::openmode mode) override
        {
            if ((static_cast<unsigned>(mode) & static_cast<unsigned>(std::ios_base::in)) == 0) {
                return pos_type(off_type(-1));
            }
            std::uint64_t base = 0;
            if (direction == std::ios_base::cur) {
                base = position();
            } else if (direction == std::ios_base::end) {
                base = payload.logical_size;
            }
            const auto signed_base = static_cast<off_type>(base);
            const auto remaining = static_cast<off_type>(payload.logical_size - base);
            if (offset < -signed_base || offset > remaining) {
                return pos_type(off_type(-1));
            }
            const std::uint64_t target = static_cast<std::uint64_t>(signed_base + offset);
            if (gptr() != nullptr) {
                const std::uint64_t buffered_end = cursor + static_cast<std::uint64_t>(egptr() - eback());
                if (target >= cursor && target <= buffered_end) {
                    auto *data = eback();
                    setg(data, data + static_cast<std::ptrdiff_t>(target - cursor), egptr());
                    return pos_type(static_cast<off_type>(target));
                }
            }
            cursor = target;
            setg(nullptr, nullptr, nullptr);
            return pos_type(static_cast<off_type>(target));
        }

        pos_type seekpos(const pos_type at, const std::ios_base::openmode mode) override
        {
            return seekoff(static_cast<off_type>(at), std::ios_base::beg, mode);
        }

        encrypted_payload payload;
        io::bounded_stream input;
        codecs::aes256_cfb128 cipher;
        std::vector<std::byte> buffer;
        std::uint64_t cursor = 0;
        std::uint64_t cipher_next = 0;
        bool cipher_cursor_valid = false;
        bool verified = false;
    };

    verified_payload_source::verified_payload_source(std::istream &stream, const encrypted_payload &payload,
                                                     const codecs::key256 &key)
        : state_(std::make_unique<state>(stream, payload, key)), view_(state_.get())
    {
    }

    verified_payload_source::~verified_payload_source() = default;

    void verified_payload_source::verify(const std::function<void(std::uint64_t)> &progress)
    {
        state_->verify(progress);
        view_.clear();
    }

    bool verified_payload_source::verified() const noexcept
    {
        return state_->verified;
    }

    std::istream &verified_payload_source::stream()
    {
        if (!state_->verified) {
            throw encryption_error("encrypted plaintext has not been verified");
        }
        return view_;
    }

    std::istream &verified_payload_source::unverified_stream() noexcept
    {
        return view_;
    }
} // namespace unpackrat::godot
