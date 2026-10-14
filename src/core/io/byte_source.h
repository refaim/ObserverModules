#ifndef UNPACKRAT_CORE_IO_BYTE_SOURCE_H
#define UNPACKRAT_CORE_IO_BYTE_SOURCE_H

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>
#include <stdexcept>

namespace unpackrat::io
{
    // A borrowed random-access logical byte stream. read fills the requested span exactly or
    // throws; callers validate ranges against size before invoking it.
    struct byte_source final
    {
        std::uint64_t size = 0;
        std::function<void(std::uint64_t, std::span<std::byte>)> read;
    };

    using byte_sink = std::function<void(std::span<const std::byte>)>;

    inline void copy_byte_range(const byte_source &source, const std::uint64_t offset, const std::uint64_t count,
                                const byte_sink &sink)
    {
        if (offset > source.size || count > source.size - offset) {
            throw std::out_of_range("byte source range exceeds its logical extent");
        }
        std::array<std::byte, 4096> chunk{};
        std::uint64_t copied = 0;
        while (copied < count) {
            const std::size_t length =
                static_cast<std::size_t>((std::min)(count - copied, static_cast<std::uint64_t>(chunk.size())));
            source.read(offset + copied, std::span<std::byte>(chunk.data(), length));
            sink(std::span<const std::byte>(chunk.data(), length));
            copied += length;
        }
    }
} // namespace unpackrat::io
#endif
