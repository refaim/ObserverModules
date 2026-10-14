#ifndef UNPACKRAT_CORE_GODOT_RSCC_H
#define UNPACKRAT_CORE_GODOT_RSCC_H

#include "../codecs/resource_compression.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <span>
#include <stdexcept>
#include <vector>

// Platform-neutral reader for the Godot "RSCC" block-compressed stream used by
// compressed binary resources. It returns the logical decompressed payload only and
// never prepends an "RSRC" wrapper or shifts a resource offset: the caller feeds the
// result to the binary resource reader with `has_magic = false`.
//
// The container layout is adapted from the canonical Godot compressed file access,
// distributed under the MIT license:
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
// Sources pinned for traceability (commit, path, sha256 in the reference bundle):
//   godotengine/godot @ 7249992b62bbe8ba01df41df1bb1b6122d1f2ba4 (2.1.6-stable)
//     core/io/file_access_compressed.{h,cpp}: 4-byte magic, u32 mode, u32 block size,
//     u32 total, (total / block_size) + 1 u32 block sizes, trailing magic.
//   godotengine/godot @ de2f0f147c5b7eff2d0f6dbc35042a4173fd59be (3.6-stable)
//     core/io/file_access_compressed.{h,cpp}: zero block size rejected.
//   godotengine/godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d (4.5-stable)
//     core/io/file_access_compressed.{h,cpp}: same layout; the last block carries
//     `total % block_size` logical bytes, so an exact multiple yields a zero-length
//     tail block.
//   godotengine/godot @ 232e6b14abf6e590b41d60f11704e42de22bff72 (master, 2026-10-09)
//     core/io/file_access_compressed.{h,cpp}: unchanged layout.
// Only the verified wire semantics are reused; the reader is re-implemented over a
// bounded span with checked counts, offsets and cumulative sizes before any
// allocation, and a strict "reject, never warn-and-continue" policy. The canonical
// writer appends the 4-byte magic after the last block, and this reader requires it so
// that trailing bytes are rejected rather than silently ignored.

namespace unpackrat::godot
{
    // Ceilings enforced before any reservation, seek or decompression allocation. Every
    // default bounds the whole logical stream; a caller may tighten any of them.
    struct rscc_limits final
    {
        std::uint64_t max_input_bytes = 256ULL * 1024ULL * 1024ULL;
        std::uint64_t max_output_bytes = 256ULL * 1024ULL * 1024ULL;
        std::uint64_t max_block_bytes = 16ULL * 1024ULL * 1024ULL;
        std::uint64_t max_blocks = 65536;
    };

    class rscc_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Decompresses one complete "RSCC" stream into its logical payload. The call fails
    // with `rscc_error` for a missing header or trailing magic, a zero block size, an
    // unsupported compression mode, a block count, table, offset or cumulative size
    // that runs past the stored bytes, or any block that does not decompress to its
    // exact logical size.
    [[nodiscard]] std::vector<std::byte> decompress_rscc(std::span<const std::byte> input,
                                                         const rscc_limits &limits = {});

    // Inspects the fixed "RSCC" header and envelope without decompressing any block or
    // allocating the decoded payload, so a caller can charge one aggregate budget for a
    // stream (including a later failed decode) before it commits to decompression. The
    // call validates, in order, the input budget, the 16 byte minimum, the header and
    // trailing magic, a nonzero block size, a supported mode, the declared logical size
    // against `max_output_bytes`, the block count against `max_blocks`, and that the
    // block table fits before the trailing magic. It returns the declared total logical
    // byte count.
    //
    // Only the header and envelope are validated: the compressed payload is never
    // touched, so a stream whose blocks are corrupt still reports its declared size here
    // and then fails with `rscc_error` from `decompress_rscc`. Every rejected header or
    // envelope throws `rscc_error`, exactly as `decompress_rscc` rejects it.
    [[nodiscard]] std::uint64_t rscc_logical_size(std::span<const std::byte> input, const rscc_limits &limits = {});

    // Random-access logical view for classification. It validates the fixed envelope and loads
    // only the compressed prefixes needed for touched blocks. Media blocks skipped by the binary
    // resource parser remain compressed until extraction.
    class rscc_metadata_reader final
    {
      public:
        rscc_metadata_reader(std::uint64_t stored_size,
                             std::function<void(std::uint64_t, std::span<std::byte>)> read_stored,
                             const rscc_limits &limits = {});
        [[nodiscard]] std::uint64_t logical_size() const noexcept
        {
            return total_;
        }
        void read(std::uint64_t offset, std::span<std::byte> destination);
        // Extraction validates Godot's mandatory zero-logical terminal block here.
        void validate_terminal_block();

      private:
        void load(std::uint64_t index);

        std::function<void(std::uint64_t, std::span<std::byte>)> read_stored_;
        rscc_limits limits_;
        codecs::compression_kind kind_ = codecs::compression_kind::fastlz;
        std::uint64_t block_size_ = 0;
        std::uint64_t total_ = 0;
        std::uint64_t block_count_ = 0;
        std::uint64_t data_start_ = 0;
        std::uint64_t data_end_ = 0;
        std::uint64_t next_index_ = 0;
        std::uint64_t next_offset_ = 0;
        std::uint64_t cached_index_ = (std::numeric_limits<std::uint64_t>::max)();
        std::unique_ptr<codecs::block_stream_decoder> streamed_block_;
    };
} // namespace unpackrat::godot
#endif
