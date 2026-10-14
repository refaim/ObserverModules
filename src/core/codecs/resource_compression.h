#ifndef UNPACKRAT_CORE_CODECS_RESOURCE_COMPRESSION_H
#define UNPACKRAT_CORE_CODECS_RESOURCE_COMPRESSION_H

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <span>
#include <stdexcept>
#include <vector>

// Platform-neutral decompression wrapper for the block codecs used by Godot
// compressed resources. It exposes only unpackrat types and errors; the external
// zlib, zstd, fastlz and brotli headers, types and error codes stay in the
// implementation file.
//
// The wire semantics are adapted from the canonical Godot block codecs, distributed
// under the MIT license:
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
// Sources pinned for traceability (commit, path, sha256 in the reference bundle):
//   godotengine/godot @ 7249992b62bbe8ba01df41df1bb1b6122d1f2ba4 (2.1.6-stable)
//     core/io/compression.{h,cpp}: Compression::Mode order and FastLZ/Deflate/GZip
//     window selection.
//   godotengine/godot @ de2f0f147c5b7eff2d0f6dbc35042a4173fd59be (3.6-stable)
//     core/io/compression.{h,cpp}: unchanged mode order.
//   godotengine/godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d (4.5-stable)
//     core/io/compression.{h,cpp}: MODE_BROTLI decode and the FastLZ output
//     buffer padding below 16 bytes.
//   godotengine/godot @ 232e6b14abf6e590b41d60f11704e42de22bff72 (master, 2026-10-09)
//     core/io/compression.{h,cpp}: unchanged mode order and FastLZ/Brotli handling.
// Only the verified wire semantics are reused; the decoder is re-implemented over a
// bounded span with checked budgets and a strict "reject, never warn-and-continue"
// policy.

namespace unpackrat::codecs
{
    // On-disk Compression::Mode values. The numeric order is part of the wire format.
    // Keep unknown 32-bit wire values intact so they cannot truncate into a supported mode.
    // NOLINTNEXTLINE(performance-enum-size)
    enum class compression_kind : std::uint32_t
    {
        fastlz = 0,
        deflate = 1,
        zstd = 2,
        gzip = 3,
        brotli = 4,
    };

    inline constexpr std::size_t default_max_output_bytes = 256ULL * 1024ULL * 1024ULL;

    class compression_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Decompresses one independently compressed block to exactly `expected_size`
    // bytes. `input` is a single raw codec stream (zlib-wrapped DEFLATE, GZip, Zstd,
    // FastLZ or Brotli), never a container. The call fails with `compression_error`
    // for an unknown mode, a truncated, corrupt or trailing-padded stream, or any
    // output whose size differs from `expected_size`. `max_output_bytes` bounds both
    // the accepted input and the requested output before any allocation.
    [[nodiscard]] std::vector<std::byte> decompress_block(std::span<const std::byte> input, compression_kind kind,
                                                          std::size_t expected_size,
                                                          std::size_t max_output_bytes = default_max_output_bytes);

    // Random reads over one block with bounded input/output scratch. A backward seek restarts the
    // decoder; adjacent forward reads keep its state. The compressed source is borrowed.
    class block_stream_decoder final
    {
      public:
        block_stream_decoder(compression_kind kind, std::uint64_t compressed_size, std::uint64_t logical_size,
                             std::function<void(std::uint64_t, std::span<std::byte>)> source);
        ~block_stream_decoder();
        block_stream_decoder(const block_stream_decoder &) = delete;
        block_stream_decoder &operator=(const block_stream_decoder &) = delete;
        void read(std::uint64_t offset, std::span<std::byte> destination);
        // Drain and validate a block even when it has no caller-visible logical bytes.
        void finish();

      private:
        struct state;
        std::unique_ptr<state> state_;
    };
} // namespace unpackrat::codecs
#endif
