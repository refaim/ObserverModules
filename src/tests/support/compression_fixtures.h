#pragma once

#include "../../core/codecs/resource_compression.h"

#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

// Test-only reference encoders. They mirror the canonical Godot block writer so the
// production decoder can be exercised against real streams for every mode, including
// the FastLZ short-input padding and the "RSCC" container with a zero-length tail
// block. These helpers link the compression side of each library, which production
// code never does.
namespace test::support
{
    // Compresses one block exactly like Godot's FileAccessCompressed writer.
    [[nodiscard]] std::vector<std::byte> compress_block(std::span<const std::byte> input,
                                                        unpackrat::codecs::compression_kind kind);

    // Builds a complete "RSCC" stream: magic, u32 mode, u32 block size, u32 total,
    // one u32 compressed size per block (including the zero-length tail of an exact
    // multiple), the blocks, then the trailing magic.
    [[nodiscard]] std::vector<std::byte> build_rscc(std::span<const std::byte> logical,
                                                    unpackrat::codecs::compression_kind kind, std::uint32_t block_size);

    // A deterministic, mildly compressible payload of the requested length.
    [[nodiscard]] std::vector<std::byte> make_payload(std::size_t size);
} // namespace test::support
