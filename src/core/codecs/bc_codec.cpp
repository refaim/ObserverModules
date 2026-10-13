#include "bc_codec.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <limits>

// Vendored, unmodified bcdec is a self-contained decoder with no external dependencies. Building
// it with internal linkage here keeps its symbols local to this translation unit; BCDECDEF is
// overridden to static inline so the BC6H and precise-mode helpers that this adapter does not use
// (those families are unsupported) do not trip the unused-function warning under either toolset.
#define BCDECDEF static inline
#define BCDEC_IMPLEMENTATION
#pragma warning(push, 0)
#include <../third_party/bcdec/bcdec.h>
#pragma warning(pop)

namespace unpackrat::codecs
{
    namespace
    {
        constexpr std::uint32_t block_edge = 4;
        constexpr std::size_t block_pixels = std::size_t{block_edge} * block_edge;
        constexpr std::size_t rgba_channels = 4;
        constexpr std::size_t max_block_bytes = 16;

        // Maps a supported family to its compressed block size, rejecting any other value.
        std::size_t bc_block_bytes(const bc_format format)
        {
            if (format == bc_format::bc1 || format == bc_format::bc4) {
                return BCDEC_BC1_BLOCK_SIZE;
            }
            if (format == bc_format::bc2 || format == bc_format::bc3 || format == bc_format::bc5 ||
                format == bc_format::bc7) {
                return BCDEC_BC2_BLOCK_SIZE;
            }
            throw image_error("unsupported block-compression format");
        }

        // Decodes one aligned compressed block into a 4x4 RGBA block. bcdec emits RGBA for BC1/2/3/7;
        // BC4 and BC5 come out as raw R and RG channels and are expanded to RGBA here, matching
        // Godot's R8/RG8 semantics ((R, 0, 0, 255) and (R, G, 0, 255)) without inventing detail.
        // The trailing branch handles BC7; bc_block_bytes has already rejected any other family, so
        // no runtime default is needed.
        void decode_block(const std::byte *compressed, const bc_format format, std::byte *const rgba) noexcept
        {
            const int rgba_pitch = static_cast<int>(block_edge * rgba_channels);
            if (format == bc_format::bc1) {
                bcdec_bc1(compressed, rgba, rgba_pitch);
            } else if (format == bc_format::bc2) {
                bcdec_bc2(compressed, rgba, rgba_pitch);
            } else if (format == bc_format::bc3) {
                bcdec_bc3(compressed, rgba, rgba_pitch);
            } else if (format == bc_format::bc4) {
                std::array<std::byte, block_pixels> plane{};
                bcdec_bc4(compressed, plane.data(), static_cast<int>(block_edge));
                for (std::size_t index = 0; index < plane.size(); ++index) {
                    rgba[index * rgba_channels] = plane[index];
                    rgba[index * rgba_channels + 1] = std::byte{0};
                    rgba[index * rgba_channels + 2] = std::byte{0};
                    rgba[index * rgba_channels + 3] = std::byte{0xff};
                }
            } else if (format == bc_format::bc5) {
                std::array<std::byte, block_pixels * 2> plane{};
                bcdec_bc5(compressed, plane.data(), static_cast<int>(block_edge * 2));
                for (std::size_t index = 0; index < block_pixels; ++index) {
                    rgba[index * rgba_channels] = plane[index * 2];
                    rgba[index * rgba_channels + 1] = plane[index * 2 + 1];
                    rgba[index * rgba_channels + 2] = std::byte{0};
                    rgba[index * rgba_channels + 3] = std::byte{0xff};
                }
            } else {
                bcdec_bc7(compressed, rgba, rgba_pitch);
            }
        }
    } // namespace

    rgba_image decode_bc(const std::span<const std::byte> input, const bc_format format, const std::uint32_t width,
                         const std::uint32_t height, const image_limits &limits)
    {
        if (width == 0 || height == 0) {
            throw image_error("BC dimensions must be non-zero");
        }
        const std::size_t block_bytes = bc_block_bytes(format);

        const auto blocks_x = (static_cast<std::uint64_t>(width) + (block_edge - 1)) / block_edge;
        const auto blocks_y = (static_cast<std::uint64_t>(height) + (block_edge - 1)) / block_edge;
        const auto block_count = blocks_x * blocks_y; // at most 2^60, so the product cannot overflow

        constexpr auto max_size = (std::numeric_limits<std::uint64_t>::max)();
        // The whole block grid must be representable, and the decoded extent and byte count are
        // validated before any offset or buffer size is derived from the untrusted dimensions.
        if (block_count > max_size / block_bytes) {
            throw image_error("BC input size overflows");
        }

        const auto pixel_count = static_cast<std::uint64_t>(width) * static_cast<std::uint64_t>(height);
        if (pixel_count > limits.max_pixels) {
            throw image_error("BC image exceeds the configured pixel budget");
        }
        if (pixel_count > max_size / rgba_channels) {
            throw image_error("BC output size overflows");
        }
        const auto output_bytes = pixel_count * rgba_channels;
        if (output_bytes > limits.max_output_bytes) {
            throw image_error("BC image exceeds the configured output budget");
        }

        if (block_count * block_bytes != input.size()) {
            throw image_error("BC input length does not match the block layout");
        }

        if constexpr (sizeof(std::size_t) < sizeof(std::uint64_t)) {
            if (output_bytes > (std::numeric_limits<std::size_t>::max)()) {
                throw image_error("BC output buffer is not addressable");
            }
        }

        rgba_image decoded;
        decoded.width = width;
        decoded.height = height;
        decoded.pixels.resize(static_cast<std::size_t>(output_bytes));

        for (std::uint64_t block_y = 0; block_y < blocks_y; ++block_y) {
            for (std::uint64_t block_x = 0; block_x < blocks_x; ++block_x) {
                // The caller's payload may be misaligned, so the block is copied into an aligned
                // buffer before the decoder reads it, and the decoded block lands in one too.
                alignas(16) std::array<std::byte, max_block_bytes> compressed{};
                const auto offset = static_cast<std::size_t>((block_y * blocks_x + block_x) * block_bytes);
                std::memcpy(compressed.data(), input.data() + offset, block_bytes);

                alignas(16) std::array<std::byte, block_pixels * rgba_channels> block{};
                decode_block(compressed.data(), format, block.data());

                const auto origin_x = static_cast<std::uint32_t>(block_x * block_edge);
                const auto origin_y = static_cast<std::uint32_t>(block_y * block_edge);
                const auto span_x = std::min(block_edge, width - origin_x);
                const auto span_y = std::min(block_edge, height - origin_y);
                for (std::uint32_t row = 0; row < span_y; ++row) {
                    const auto *source = block.data() + static_cast<std::size_t>(row) * block_edge * rgba_channels;
                    auto *destination = decoded.pixels.data() +
                                        (static_cast<std::size_t>(origin_y + row) * width + origin_x) * rgba_channels;
                    std::memcpy(destination, source, static_cast<std::size_t>(span_x) * rgba_channels);
                }
            }
        }
        return decoded;
    }
} // namespace unpackrat::codecs
