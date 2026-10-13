#include "compression_fixtures.h"

#include <algorithm>
#include <cstdint>
#include <stdexcept>

#include <brotli/encode.h>
#include <fastlz.h>
#include <zlib.h>
#include <zstd.h>

namespace test::support
{
    namespace
    {
        constexpr std::uint32_t rscc_magic = 0x43435352U; // "RSCC" little-endian

        [[noreturn]] void fail(const char *message)
        {
            throw std::runtime_error(message);
        }

        std::vector<std::byte> compress_fastlz(const std::span<const std::byte> input)
        {
            std::vector<std::byte> padded;
            std::span<const std::byte> source = input;
            if (source.size() < 16) { // FastLZ minimum block, padded with NUL like the writer.
                padded.assign(16, std::byte{0});
                std::copy(source.begin(), source.end(), padded.begin());
                source = padded;
            }
            std::size_t capacity = source.size() + source.size() * 6 / 100;
            capacity = std::max<std::size_t>(capacity, 66);
            std::vector<std::byte> output(capacity);
            const int produced = fastlz_compress(source.data(), static_cast<int>(source.size()), output.data());
            if (produced <= 0) {
                fail("fastlz fixture compression failed");
            }
            output.resize(static_cast<std::size_t>(produced));
            return output;
        }

        std::vector<std::byte> compress_stream(const std::span<const std::byte> input, const int window_bits)
        {
            z_stream stream{};
            if (deflateInit2(&stream, Z_DEFAULT_COMPRESSION, Z_DEFLATED, window_bits, 8, Z_DEFAULT_STRATEGY) != Z_OK) {
                fail("zlib fixture initialization failed");
            }
            std::vector<std::byte> output(deflateBound(&stream, static_cast<uLong>(input.size())) + 64);
            stream.next_in = reinterpret_cast<Bytef *>(const_cast<std::byte *>(input.data()));
            stream.avail_in = static_cast<uInt>(input.size());
            stream.next_out = reinterpret_cast<Bytef *>(output.data());
            stream.avail_out = static_cast<uInt>(output.size());
            const int result = deflate(&stream, Z_FINISH);
            if (result != Z_STREAM_END) {
                static_cast<void>(deflateEnd(&stream));
                fail("zlib fixture compression failed");
            }
            output.resize(output.size() - static_cast<std::size_t>(stream.avail_out));
            static_cast<void>(deflateEnd(&stream));
            return output;
        }

        std::vector<std::byte> compress_zstd(const std::span<const std::byte> input)
        {
            std::vector<std::byte> output(ZSTD_compressBound(input.size()));
            const std::size_t produced = ZSTD_compress(output.data(), output.size(), input.data(), input.size(), 3);
            if (ZSTD_isError(produced)) {
                fail("zstd fixture compression failed");
            }
            output.resize(produced);
            return output;
        }

        std::vector<std::byte> compress_brotli(const std::span<const std::byte> input)
        {
            std::size_t capacity = BrotliEncoderMaxCompressedSize(input.size());
            if (capacity == 0) {
                capacity = 16;
            }
            std::vector<std::byte> output(capacity);
            std::size_t produced = output.size();
            const BROTLI_BOOL ok =
                BrotliEncoderCompress(BROTLI_DEFAULT_QUALITY, BROTLI_DEFAULT_WINDOW, BROTLI_MODE_GENERIC, input.size(),
                                      reinterpret_cast<const std::uint8_t *>(input.data()), &produced,
                                      reinterpret_cast<std::uint8_t *>(output.data()));
            if (ok == BROTLI_FALSE) {
                fail("brotli fixture compression failed");
            }
            output.resize(produced);
            return output;
        }
    } // namespace

    std::vector<std::byte> compress_block(const std::span<const std::byte> input,
                                          const unpackrat::codecs::compression_kind kind)
    {
        switch (kind) {
        case unpackrat::codecs::compression_kind::fastlz:
            return compress_fastlz(input);
        case unpackrat::codecs::compression_kind::deflate:
            return compress_stream(input, 15);
        case unpackrat::codecs::compression_kind::gzip:
            return compress_stream(input, 15 + 16);
        case unpackrat::codecs::compression_kind::zstd:
            return compress_zstd(input);
        case unpackrat::codecs::compression_kind::brotli:
            return compress_brotli(input);
        }
        fail("unsupported fixture compression mode");
    }

    std::vector<std::byte> build_rscc(const std::span<const std::byte> logical,
                                      const unpackrat::codecs::compression_kind kind, const std::uint32_t block_size)
    {
        const std::uint32_t total = static_cast<std::uint32_t>(logical.size());
        const std::uint32_t block_count = total / block_size + 1;

        std::vector<std::vector<std::byte>> blocks;
        blocks.reserve(block_count);
        for (std::uint32_t index = 0; index < block_count; ++index) {
            const std::uint32_t begin = index * block_size;
            const std::uint32_t length = (index + 1 == block_count) ? total % block_size : block_size;
            blocks.push_back(compress_block(logical.subspan(begin, length), kind));
        }

        std::vector<std::byte> output;
        const auto put_u32 = [&output](const std::uint32_t value) {
            for (int shift = 0; shift < 32; shift += 8) {
                output.push_back(static_cast<std::byte>((value >> shift) & 0xFFU));
            }
        };
        put_u32(rscc_magic);
        put_u32(static_cast<std::uint32_t>(kind));
        put_u32(block_size);
        put_u32(total);
        for (const auto &block : blocks) {
            put_u32(static_cast<std::uint32_t>(block.size()));
        }
        for (const auto &block : blocks) {
            output.insert(output.end(), block.begin(), block.end());
        }
        put_u32(rscc_magic);
        return output;
    }

    std::vector<std::byte> make_payload(const std::size_t size)
    {
        std::vector<std::byte> payload(size);
        std::uint32_t state = 0x9E3779B9U;
        for (auto &byte : payload) {
            state = state * 1664525U + 1013904223U;
            byte = static_cast<std::byte>((state >> 24) & 0x0FU); // low entropy keeps streams small
        }
        return payload;
    }
} // namespace test::support
