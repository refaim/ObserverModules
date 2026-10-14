#ifndef UNPACKRAT_CORE_COMPRESSION_ZLIB_CODEC_H
#define UNPACKRAT_CORE_COMPRESSION_ZLIB_CODEC_H

#include <cstddef>
#include <span>
#include <stdexcept>
#include <vector>

namespace unpackrat::compression
{
    class error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    [[nodiscard]] std::vector<std::byte> decompress_zlib(std::span<const std::byte> input,
                                                         std::size_t max_output_bytes);
} // namespace unpackrat::compression
#endif
