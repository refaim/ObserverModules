#include "image_resource.h"

#include "core/codecs/bc_codec.h"
#include "core/codecs/png_codec.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <limits>
#include <optional>
#include <span>
#include <string_view>
#include <vector>

// Converts the image payloads a binary resource can hold. The raw wire formats follow the pinned
// Godot image sources: the Godot 3/4 Image::Format ids and their packings (image.h / image.cpp
// get_pixel, get_format_name and _get_dst_image_size) and the Godot 2 embedded Image variant ids
// and packings (resource_format_binary.cpp IMAGE_FORMAT_*, image.cpp get_pixel). Only the verified
// formats are decoded; any other format stays unsupported and keeps the raw payload upstream.

namespace unpackrat::godot
{
    namespace
    {
        using codecs::bc_format;
        using codecs::image_error;
        using codecs::image_limits;
        using codecs::rgba_image;

        constexpr std::uint32_t engine_major_godot2 = 2;
        constexpr std::uint32_t embedded_encoding_raw = 1;
        constexpr std::uint32_t embedded_encoding_lossless = 2;
        constexpr std::uint32_t embedded_encoding_lossy = 3;

        constexpr std::uint64_t rgba_channels = 4;
        // Our bound on the additional mip levels a Godot 2 raw image may declare. The engine keeps
        // emitting clamped minimum-size levels for as many levels as are declared, so the count is
        // bounded here rather than derived from a geometric limit.
        constexpr std::uint32_t max_godot2_extra_levels = 32;

        constexpr std::string_view extension_png = ".png";
        constexpr std::string_view extension_webp = ".webp";
        // The Godot 2 lossless payload is the 4-byte "PNG " marker followed by a libpng stream.
        constexpr std::string_view png_marker = "PNG ";
        constexpr std::string_view png_signature = std::string_view("\x89PNG\r\n\x1A\n", 8);
        // The Godot 2 lossy payload is the 4-byte "WEBP" marker followed by a RIFF/WEBP file.
        constexpr std::string_view webp_marker = "WEBP";
        constexpr std::string_view riff_marker = "RIFF";
        constexpr std::string_view webp_fourcc = "WEBP";
        constexpr std::size_t webp_header_bytes = 12;
        // The RIFF header is the "RIFF" fourcc plus a 32-bit size field; the size counts the bytes
        // after it, so the smallest valid value still spans the 4-byte "WEBP" form type.
        constexpr std::size_t webp_riff_header_bytes = 8;
        constexpr std::size_t webp_form_bytes = 4;

        std::uint8_t byte_value(const std::byte *source) noexcept
        {
            return static_cast<std::uint8_t>(source[0]);
        }

        std::uint16_t little_endian_u16(const std::byte *source) noexcept
        {
            const std::uint16_t low = byte_value(source);
            const std::uint16_t high = static_cast<std::uint16_t>(byte_value(source + 1) << 8U);
            return static_cast<std::uint16_t>(low | high);
        }

        std::uint32_t little_endian_u32(const std::byte *source) noexcept
        {
            return static_cast<std::uint32_t>(byte_value(source)) |
                   (static_cast<std::uint32_t>(byte_value(source + 1)) << 8U) |
                   (static_cast<std::uint32_t>(byte_value(source + 2)) << 16U) |
                   (static_cast<std::uint32_t>(byte_value(source + 3)) << 24U);
        }

        // ---- one output RGBA quad per source pixel ----
        using pixel_reader = void (*)(const std::byte *source, std::uint8_t (&rgba)[4]);

        void read_gray(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            const std::uint8_t value = byte_value(source);
            rgba[0] = value;
            rgba[1] = value;
            rgba[2] = value;
            rgba[3] = 0xFF;
        }

        void read_gray_alpha(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            const std::uint8_t value = byte_value(source);
            rgba[0] = value;
            rgba[1] = value;
            rgba[2] = value;
            rgba[3] = byte_value(source + 1);
        }

        void read_intensity(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            rgba[0] = 0xFF;
            rgba[1] = 0xFF;
            rgba[2] = 0xFF;
            rgba[3] = byte_value(source);
        }

        void read_rgb(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            rgba[0] = byte_value(source);
            rgba[1] = byte_value(source + 1);
            rgba[2] = byte_value(source + 2);
            rgba[3] = 0xFF;
        }

        void read_rgba(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            rgba[0] = byte_value(source);
            rgba[1] = byte_value(source + 1);
            rgba[2] = byte_value(source + 2);
            rgba[3] = byte_value(source + 3);
        }

        void read_r8(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            rgba[0] = byte_value(source);
            rgba[1] = 0;
            rgba[2] = 0;
            rgba[3] = 0xFF;
        }

        void read_rg8(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            rgba[0] = byte_value(source);
            rgba[1] = byte_value(source + 1);
            rgba[2] = 0;
            rgba[3] = 0xFF;
        }

        // Godot 3's RGBA5551 get_pixel divides each 5-bit field by 15 before scaling by 255; that
        // is exactly a multiply by 17 saturated at 255, reproduced so the output matches the engine.
        std::uint8_t scale_5_to_8(const std::uint32_t value) noexcept
        {
            const std::uint32_t scaled = value * 17U;
            return static_cast<std::uint8_t>(scaled > 255U ? 255U : scaled);
        }

        void read_rgba4444(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            const std::uint32_t packed = little_endian_u16(source);
            rgba[0] = static_cast<std::uint8_t>(((packed >> 12U) & 0xFU) * 17U);
            rgba[1] = static_cast<std::uint8_t>(((packed >> 8U) & 0xFU) * 17U);
            rgba[2] = static_cast<std::uint8_t>(((packed >> 4U) & 0xFU) * 17U);
            rgba[3] = static_cast<std::uint8_t>((packed & 0xFU) * 17U);
        }

        void read_rgba5551(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            const std::uint32_t packed = little_endian_u16(source);
            rgba[0] = scale_5_to_8((packed >> 11U) & 0x1FU);
            rgba[1] = scale_5_to_8((packed >> 6U) & 0x1FU);
            rgba[2] = scale_5_to_8((packed >> 1U) & 0x1FU);
            rgba[3] = static_cast<std::uint8_t>((packed & 0x1U) * 0xFFU);
        }

        void read_rgb565(const std::byte *source, std::uint8_t (&rgba)[4]) noexcept
        {
            const std::uint32_t packed = little_endian_u16(source);
            rgba[0] = static_cast<std::uint8_t>(((packed >> 11U) & 0x1FU) * 0xFFU / 31U);
            rgba[1] = static_cast<std::uint8_t>(((packed >> 5U) & 0x3FU) * 0xFFU / 63U);
            rgba[2] = static_cast<std::uint8_t>((packed & 0x1FU) * 0xFFU / 31U);
            rgba[3] = 0xFF;
        }

        struct format_descriptor final
        {
            bool block_compressed;
            bool alpha_to_green;        // Godot 4 DXT5_RA_AS_RG stores the RG channels in R and A
            std::uint8_t element_bytes; // bytes per pixel for uncompressed formats
            std::uint8_t block;         // block edge: 1, or 4 for a block-compressed family
            std::uint8_t rshift;        // level-size right shift: 1 for BC1/BC4, else 0
            bc_format bc;               // valid for a block-compressed family
            pixel_reader read;          // non-null for an uncompressed format
        };

        constexpr format_descriptor descriptor_gray{false, false, 1, 1, 0, bc_format::bc1, &read_gray};
        constexpr format_descriptor descriptor_intensity{false, false, 1, 1, 0, bc_format::bc1, &read_intensity};
        constexpr format_descriptor descriptor_gray_alpha{false, false, 2, 1, 0, bc_format::bc1, &read_gray_alpha};
        constexpr format_descriptor descriptor_rgb{false, false, 3, 1, 0, bc_format::bc1, &read_rgb};
        constexpr format_descriptor descriptor_rgba{false, false, 4, 1, 0, bc_format::bc1, &read_rgba};
        constexpr format_descriptor descriptor_r8{false, false, 1, 1, 0, bc_format::bc1, &read_r8};
        constexpr format_descriptor descriptor_rg8{false, false, 2, 1, 0, bc_format::bc1, &read_rg8};
        constexpr format_descriptor descriptor_rgba4444{false, false, 2, 1, 0, bc_format::bc1, &read_rgba4444};
        constexpr format_descriptor descriptor_rgba5551{false, false, 2, 1, 0, bc_format::bc1, &read_rgba5551};
        constexpr format_descriptor descriptor_rgb565{false, false, 2, 1, 0, bc_format::bc1, &read_rgb565};
        constexpr format_descriptor descriptor_bc1{true, false, 1, 4, 1, bc_format::bc1, nullptr};
        constexpr format_descriptor descriptor_bc2{true, false, 1, 4, 0, bc_format::bc2, nullptr};
        constexpr format_descriptor descriptor_bc3{true, false, 1, 4, 0, bc_format::bc3, nullptr};
        constexpr format_descriptor descriptor_bc3_rg{true, true, 1, 4, 0, bc_format::bc3, nullptr};
        constexpr format_descriptor descriptor_bc4{true, false, 1, 4, 1, bc_format::bc4, nullptr};
        constexpr format_descriptor descriptor_bc5{true, false, 1, 4, 0, bc_format::bc5, nullptr};
        constexpr format_descriptor descriptor_bc7{true, false, 1, 4, 0, bc_format::bc7, nullptr};

        // The raw wire-format id to descriptor map. Godot 2 uses the embedded Image variant
        // IMAGE_FORMAT ids; Godot 3 and 4 share the Image::Format ids for the supported subset
        // (id 7 is RGBA5551 on Godot 3 and RGB565 on Godot 4).
        struct wire_entry final
        {
            std::uint32_t engine_major;
            std::uint32_t wire_format;
            const format_descriptor *descriptor;
        };

        constexpr wire_entry wire_formats[] = {
            {2, 0, &descriptor_gray},     {2, 1, &descriptor_intensity},  {2, 2, &descriptor_gray_alpha},
            {2, 3, &descriptor_rgb},      {2, 4, &descriptor_rgba},       {2, 7, &descriptor_bc1},
            {2, 8, &descriptor_bc2},      {2, 9, &descriptor_bc3},        {2, 10, &descriptor_bc4},
            {2, 11, &descriptor_bc5},     {3, 0, &descriptor_gray},       {3, 1, &descriptor_gray_alpha},
            {3, 2, &descriptor_r8},       {3, 3, &descriptor_rg8},        {3, 4, &descriptor_rgb},
            {3, 5, &descriptor_rgba},     {3, 6, &descriptor_rgba4444},   {3, 7, &descriptor_rgba5551},
            {3, 17, &descriptor_bc1},     {3, 18, &descriptor_bc2},       {3, 19, &descriptor_bc3},
            {3, 20, &descriptor_bc4},     {3, 21, &descriptor_bc5},       {3, 22, &descriptor_bc7},
            {4, 0, &descriptor_gray},     {4, 1, &descriptor_gray_alpha}, {4, 2, &descriptor_r8},
            {4, 3, &descriptor_rg8},      {4, 4, &descriptor_rgb},        {4, 5, &descriptor_rgba},
            {4, 6, &descriptor_rgba4444}, {4, 7, &descriptor_rgb565},     {4, 17, &descriptor_bc1},
            {4, 18, &descriptor_bc2},     {4, 19, &descriptor_bc3},       {4, 20, &descriptor_bc4},
            {4, 21, &descriptor_bc5},     {4, 22, &descriptor_bc7},       {4, 34, &descriptor_bc3_rg},
        };

        // The format names the engine writes into an Image data dictionary, mapped back to the
        // Image::Format id so the writer can reuse the raw decoder. Id 7 is named "RGBA5551" on
        // both generations even though Godot 4 packs it as RGB565.
        struct named_entry final
        {
            const format_descriptor *descriptor{};
            std::string_view name{};
            std::uint32_t engine_major{};
            std::uint32_t wire_format{};
        };

        constexpr named_entry named_formats[] = {
            {&descriptor_gray, "Lum8", 3, 0},
            {&descriptor_gray_alpha, "LumAlpha8", 3, 1},
            {&descriptor_r8, "Red8", 3, 2},
            {&descriptor_rg8, "RedGreen", 3, 3},
            {&descriptor_rgb, "RGB8", 3, 4},
            {&descriptor_rgba, "RGBA8", 3, 5},
            {&descriptor_rgba4444, "RGBA4444", 3, 6},
            {&descriptor_rgba5551, "RGBA5551", 3, 7},
            {&descriptor_bc1, "DXT1 RGB8", 3, 17},
            {&descriptor_bc2, "DXT3 RGBA8", 3, 18},
            {&descriptor_bc3, "DXT5 RGBA8", 3, 19},
            {&descriptor_bc4, "RGTC Red8", 3, 20},
            {&descriptor_bc5, "RGTC RedGreen8", 3, 21},
            {&descriptor_bc7, "BPTC_RGBA", 3, 22},
            {&descriptor_gray, "Lum8", 4, 0},
            {&descriptor_gray_alpha, "LumAlpha8", 4, 1},
            {&descriptor_r8, "Red8", 4, 2},
            {&descriptor_rg8, "RedGreen", 4, 3},
            {&descriptor_rgb, "RGB8", 4, 4},
            {&descriptor_rgba, "RGBA8", 4, 5},
            {&descriptor_rgba4444, "RGBA4444", 4, 6},
            {&descriptor_rgb565, "RGBA5551", 4, 7},
            {&descriptor_bc1, "DXT1 RGB8", 4, 17},
            {&descriptor_bc2, "DXT3 RGBA8", 4, 18},
            {&descriptor_bc3, "DXT5 RGBA8", 4, 19},
            {&descriptor_bc4, "RGTC Red8", 4, 20},
            {&descriptor_bc5, "RGTC RedGreen8", 4, 21},
            {&descriptor_bc7, "BPTC_RGBA", 4, 22},
            {&descriptor_bc3_rg, "FORMAT_DXT5_RA_AS_RG", 4, 34},
        };

        const format_descriptor *lookup_wire(const std::uint32_t engine_major, const std::uint32_t wire_format) noexcept
        {
            const auto found = std::find_if(
                std::begin(wire_formats), std::end(wire_formats), [engine_major, wire_format](const wire_entry &entry) {
                    return entry.engine_major == engine_major && entry.wire_format == wire_format;
                });
            return found == std::end(wire_formats) ? nullptr : found->descriptor;
        }

        const named_entry *lookup_name(const std::uint32_t engine_major, const std::string_view name) noexcept
        {
            const auto found = std::find_if(std::begin(named_formats), std::end(named_formats),
                                            [engine_major, name](const named_entry &entry) {
                                                return entry.engine_major == engine_major && entry.name == name;
                                            });
            return found == std::end(named_formats) ? nullptr : found;
        }

        // Byte size of one mip level, rounding each extent up to the block edge and shifting the
        // packed result, matching the engine's _get_dst_image_size. Overflow of the block grid or
        // the byte count is rejected rather than wrapped.
        std::uint64_t level_bytes(const std::uint32_t width, const std::uint32_t height,
                                  const format_descriptor &descriptor)
        {
            const std::uint64_t block = descriptor.block;
            const std::uint64_t padded_width = (static_cast<std::uint64_t>(width) + block - 1) / block * block;
            const std::uint64_t padded_height = (static_cast<std::uint64_t>(height) + block - 1) / block * block;
            constexpr std::uint64_t limit = (std::numeric_limits<std::uint64_t>::max)();
            if (padded_height > limit / padded_width) {
                throw image_error("image level exceeds the addressable byte count");
            }
            const std::uint64_t pixels = padded_width * padded_height;
            if (pixels > limit / descriptor.element_bytes) {
                throw image_error("image level exceeds the addressable byte count");
            }
            return (pixels * descriptor.element_bytes) >> descriptor.rshift;
        }

        // Shared raw preflight. It validates the total RGBA byte count without allocating it:
        // the optional pixel budget, 64-bit byte-count arithmetic and the caller's decoded-byte
        // budget. Both the probe and the writer run it, so a known limit is
        // rejected identically in both paths.
        void check_raw_budget(const std::uint32_t width, const std::uint32_t height, const image_limits &limits)
        {
            const std::uint64_t pixels = static_cast<std::uint64_t>(width) * height;
            if (pixels > limits.max_pixels) {
                throw image_error("image exceeds the configured pixel budget");
            }
            if (pixels > (std::numeric_limits<std::uint64_t>::max)() / rgba_channels) {
                throw image_error("decoded image byte count overflows");
            }
            if (pixels * rgba_channels > limits.max_output_bytes) {
                throw image_error("decoded image exceeds the configured output budget");
            }
        }

        // Shared raw base-layout preflight, run by both the probe and the writer. The block decoder
        // consumes a whole-block grid, so a stored base is convertible only when its byte count
        // equals the decoder's whole-block base size. A Godot 2 exact (unrounded) block edge does
        // not, so it is reported unsupported and the raw original is kept instead of a stream the
        // writer would always reject.
        [[nodiscard]] bool raw_base_convertible(const format_descriptor &descriptor, const std::uint32_t width,
                                                const std::uint32_t height, const std::uint64_t stored_bytes)
        {
            return stored_bytes == level_bytes(width, height, descriptor);
        }

        // A preserved encoded stream is emitted byte for byte, so its known length must satisfy the
        // same byte budget before the sink is ever called.
        void check_preserved_budget(const std::uint64_t preserved_bytes, const image_limits &limits)
        {
            if (preserved_bytes > limits.max_output_bytes) {
                throw image_error("preserved image exceeds the configured output budget");
            }
        }

        // True when the canonical mip chain of ``descriptor`` is exactly ``total`` bytes. A chain
        // is walked only while its growing size still fits the expected total, so a hostile
        // dimension cannot overflow or spin.
        bool chain_matches(const std::uint32_t width, const std::uint32_t height, const format_descriptor &descriptor,
                           const bool mipmaps, const std::uint64_t total)
        {
            std::uint64_t remaining = total;
            std::uint32_t level_width = width;
            std::uint32_t level_height = height;
            for (;;) {
                const std::uint64_t level = level_bytes(level_width, level_height, descriptor);
                if (level > remaining) {
                    return false;
                }
                remaining -= level;
                if (!mipmaps || (level_width == 1 && level_height == 1)) {
                    break;
                }
                level_width = level_width > 1 ? level_width >> 1U : 1;
                level_height = level_height > 1 ? level_height >> 1U : 1;
            }
            return remaining == 0;
        }

        // Godot 2 raw geometry, mirroring Image::_get_dst_image_size: the base level uses the raw
        // extent and every following level halves it, clamped to the format's minimum data size
        // (1x1 uncompressed, 4x4 for BC1-5). The engine performs no Godot 3/4 block rounding here,
        // so the level byte count is ``extent product * pixel size >> pixel shift``.
        std::uint64_t godot2_level_bytes(const std::uint32_t width, const std::uint32_t height,
                                         const std::uint32_t level, const format_descriptor &descriptor)
        {
            const std::uint64_t min_extent = descriptor.block_compressed ? 4ULL : 1ULL;
            const std::uint64_t raw_width = width;
            const std::uint64_t raw_height = height;
            const std::uint64_t level_width =
                level == 0 ? raw_width : (min_extent > (raw_width >> level) ? min_extent : (raw_width >> level));
            const std::uint64_t level_height =
                level == 0 ? raw_height : (min_extent > (raw_height >> level) ? min_extent : (raw_height >> level));
            constexpr std::uint64_t limit = (std::numeric_limits<std::uint64_t>::max)();
            // Both extents are at most 2^32-1, so their product always fits an unsigned 64-bit value;
            // only the byte-count multiply needs a guard.
            const std::uint64_t pixels = level_width * level_height;
            if (pixels > limit / descriptor.element_bytes) {
                throw image_error("image level exceeds the addressable byte count");
            }
            return (pixels * descriptor.element_bytes) >> descriptor.rshift;
        }

        // Exact Godot 2 chain size for ``additional_levels`` extra levels beyond the base. The
        // declared count is bounded by our resource limit and every level is summed with checked
        // arithmetic so a hostile dimension cannot overflow the total.
        std::uint64_t godot2_total_bytes(const std::uint32_t width, const std::uint32_t height,
                                         const format_descriptor &descriptor, const std::uint32_t additional_levels)
        {
            if (additional_levels > max_godot2_extra_levels) {
                throw image_error("image declares more mip levels than the resource bound allows");
            }
            constexpr std::uint64_t limit = (std::numeric_limits<std::uint64_t>::max)();
            std::uint64_t total = 0;
            for (std::uint32_t level = 0; level <= additional_levels; ++level) {
                const std::uint64_t bytes = godot2_level_bytes(width, height, level, descriptor);
                if (bytes > limit - total) {
                    throw image_error("image mip chain exceeds the addressable byte count");
                }
                total += bytes;
            }
            return total;
        }

        struct image_source final
        {
            bool raw = false;
            std::string_view extension{};
            std::span<const std::byte> preserved{};
            std::uint64_t preserved_bytes = 0;
            const format_descriptor *descriptor = nullptr; // set for a raw source; the decoded format
            std::uint32_t engine_major = 0;
            std::uint32_t wire_format = 0;
            std::uint32_t width = 0;
            std::uint32_t height = 0;
            std::span<const std::byte> top_mip{};
            std::uint64_t top_mip_bytes = 0;
            std::uint64_t media_offset = 0;
            std::uint64_t media_bytes = 0;
            std::size_t media_skip = 0;
        };

        const resource_value *find_property(const resource_record &record, const std::string_view name)
        {
            const auto found = std::find_if(record.properties.begin(), record.properties.end(),
                                            [name](const resource_property &entry) { return entry.name == name; });
            return found == record.properties.end() ? nullptr : &found->value;
        }

        bool begins_with(const std::span<const std::byte> data, const std::string_view marker) noexcept
        {
            if (data.size() < marker.size()) {
                return false;
            }
            for (std::size_t index = 0; index < marker.size(); ++index) {
                if (byte_value(data.data() + index) != static_cast<std::uint8_t>(marker[index])) {
                    return false;
                }
            }
            return true;
        }

        const resource_value &require_field(const resource_value &dictionary, const std::string_view name,
                                            const value_kind kind)
        {
            for (const resource_property &entry : dictionary.dictionary) {
                if (entry.name == name) {
                    if (entry.value.kind != kind) {
                        throw image_error("image data field has an unexpected type");
                    }
                    return entry.value;
                }
            }
            throw image_error("image data field is missing");
        }

        std::uint32_t to_dimension(const std::int64_t value)
        {
            if (value <= 0 || value > 0xFFFFFFFFLL) {
                throw image_error("image dimension is out of range");
            }
            return static_cast<std::uint32_t>(value);
        }

        std::uint64_t media_length(const std::vector<std::byte> &prefix, const std::uint64_t declared,
                                   const bool metadata_only)
        {
            if (metadata_only && declared < prefix.size()) {
                throw image_error("declared image length is shorter than its retained prefix");
            }
            return metadata_only ? declared : prefix.size();
        }

        image_source raw_source(const format_descriptor &descriptor, const std::uint32_t engine_major,
                                const std::uint32_t wire_format, const std::uint32_t width, const std::uint32_t height,
                                const std::span<const std::byte> data, const std::uint64_t base_bytes,
                                const std::uint64_t media_offset, const std::uint64_t media_bytes)
        {
            image_source source;
            source.raw = true;
            source.extension = extension_png;
            source.descriptor = &descriptor;
            source.engine_major = engine_major;
            source.wire_format = wire_format;
            source.width = width;
            source.height = height;
            source.top_mip = data.first(static_cast<std::size_t>(std::min<std::uint64_t>(base_bytes, data.size())));
            source.top_mip_bytes = base_bytes;
            source.media_offset = media_offset;
            source.media_bytes = media_bytes;
            return source;
        }

        std::optional<image_source> preserved_source(const std::vector<std::byte> &data,
                                                     const std::uint64_t declared_bytes,
                                                     const std::uint64_t media_offset, const std::string_view marker,
                                                     const std::string_view signature, const std::string_view extension)
        {
            const std::span<const std::byte> payload(data);
            if (!begins_with(payload, marker)) {
                throw image_error("embedded stream is missing its encoding marker");
            }
            const std::span<const std::byte> inner = payload.subspan(marker.size());
            if (!begins_with(inner, signature)) {
                throw image_error("embedded stream does not begin with the expected signature");
            }
            image_source source;
            source.extension = extension;
            source.preserved = inner;
            source.preserved_bytes = declared_bytes - marker.size();
            source.media_offset = media_offset;
            source.media_bytes = declared_bytes;
            source.media_skip = marker.size();
            return source;
        }

        std::optional<image_source> preserved_webp_source(const std::vector<std::byte> &data,
                                                          const std::uint64_t declared_bytes,
                                                          const std::uint64_t media_offset)
        {
            const std::span<const std::byte> payload(data);
            if (!begins_with(payload, webp_marker)) {
                throw image_error("embedded WebP is missing its marker");
            }
            const std::span<const std::byte> inner = payload.subspan(webp_marker.size());
            if (inner.size() < webp_header_bytes || !begins_with(inner, riff_marker) ||
                !begins_with(inner.subspan(webp_riff_header_bytes), webp_fourcc)) {
                throw image_error("embedded WebP header is malformed");
            }
            const std::uint32_t riff_size = little_endian_u32(inner.data() + 4);
            if (riff_size < webp_form_bytes ||
                static_cast<std::uint64_t>(riff_size) + webp_riff_header_bytes > declared_bytes - webp_marker.size()) {
                throw image_error("embedded WebP riff size is out of range");
            }
            image_source source;
            source.extension = extension_webp;
            source.preserved = inner;
            source.preserved_bytes = declared_bytes - webp_marker.size();
            source.media_offset = media_offset;
            source.media_bytes = declared_bytes;
            source.media_skip = webp_marker.size();
            return source;
        }

        // A Godot 2 embedded raw payload declares an additional-level count, not a boolean: the
        // stored chain must be exactly the engine's geometry for that count. Only the base level is
        // converted; the counted minimum-size levels after it are validated and preserved upstream.
        std::optional<image_source> legacy_raw_source(const std::uint32_t wire_format, const std::uint32_t width,
                                                      const std::uint32_t height, const std::uint32_t mipmaps,
                                                      const std::vector<std::byte> &data,
                                                      const std::uint64_t declared_bytes,
                                                      const std::uint64_t media_offset)
        {
            const format_descriptor *const descriptor = lookup_wire(engine_major_godot2, wire_format);
            if (descriptor == nullptr) {
                return std::nullopt;
            }
            if (width == 0 || height == 0) {
                throw image_error("image dimensions must be non-zero");
            }
            const std::uint64_t base = godot2_level_bytes(width, height, 0, *descriptor);
            const std::uint64_t total = godot2_total_bytes(width, height, *descriptor, mipmaps);
            if (declared_bytes != total) {
                throw image_error("raw image length does not match the declared mip chain");
            }
            return raw_source(*descriptor, engine_major_godot2, wire_format, width, height, data, base, media_offset,
                              declared_bytes);
        }

        std::optional<image_source> resolve_legacy(const resource_record &root, const bool metadata_only)
        {
            const resource_value *const image = find_property(root, "image");
            if (image == nullptr) {
                throw image_error("ImageTexture has no image property");
            }
            if (image->kind == value_kind::nil) {
                return std::nullopt;
            }
            if (image->kind != value_kind::embedded_image) {
                throw image_error("ImageTexture image property is not an image");
            }
            const embedded_image &embedded = image->image;
            const std::uint64_t declared_bytes = media_length(embedded.data, embedded.byte_length, metadata_only);
            if (embedded.encoding == embedded_encoding_raw) {
                return legacy_raw_source(embedded.format, embedded.width, embedded.height, embedded.mipmaps,
                                         embedded.data, declared_bytes, embedded.byte_offset);
            }
            if (embedded.encoding == embedded_encoding_lossless) {
                return preserved_source(embedded.data, declared_bytes, embedded.byte_offset, png_marker, png_signature,
                                        extension_png);
            }
            if (embedded.encoding == embedded_encoding_lossy) {
                return preserved_webp_source(embedded.data, declared_bytes, embedded.byte_offset);
            }
            throw image_error("embedded image encoding is unknown");
        }

        // A Godot 3/4 Image record carries its pixels in a "data" dictionary whose mip chain length
        // must match the canonical geometry; only the base level is converted.
        std::optional<image_source> modern_image_source(const std::uint32_t engine_major, const resource_record &image,
                                                        const bool metadata_only)
        {
            const resource_value *const data = find_property(image, "data");
            if (data == nullptr) {
                throw image_error("Image has no data property");
            }
            if (data->kind != value_kind::dictionary) {
                throw image_error("Image data property is not a dictionary");
            }
            const resource_value &width_field = require_field(*data, "width", value_kind::integer);
            const resource_value &height_field = require_field(*data, "height", value_kind::integer);
            const resource_value &format_field = require_field(*data, "format", value_kind::string);
            const resource_value &mipmaps_field = require_field(*data, "mipmaps", value_kind::boolean);
            const resource_value &data_field = require_field(*data, "data", value_kind::bytes);
            const named_entry *const named = lookup_name(engine_major, format_field.string);
            if (named == nullptr) {
                return std::nullopt;
            }
            const std::uint32_t width = to_dimension(width_field.integer);
            const std::uint32_t height = to_dimension(height_field.integer);
            const std::span<const std::byte> payload(data_field.bytes);
            const std::uint64_t declared_bytes = media_length(data_field.bytes, data_field.byte_length, metadata_only);
            if (!chain_matches(width, height, *named->descriptor, mipmaps_field.boolean, declared_bytes)) {
                throw image_error("image data length does not match the mip chain");
            }
            const std::uint64_t base = level_bytes(width, height, *named->descriptor);
            return raw_source(*named->descriptor, engine_major, named->wire_format, width, height, payload, base,
                              data_field.byte_offset, declared_bytes);
        }

        std::optional<image_source> resolve_modern(const std::uint32_t engine_major, const resource_record &root,
                                                   const binary_resource &resource, const bool metadata_only)
        {
            if (root.type == "Image") {
                return modern_image_source(engine_major, root, metadata_only);
            }
            const resource_value *const image = find_property(root, "image");
            if (image == nullptr) {
                throw image_error("ImageTexture has no image property");
            }
            if (image->kind != value_kind::reference) {
                throw image_error("ImageTexture image property is not an object");
            }
            const resource_reference &reference = image->reference;
            if (reference.external || reference.inline_definition || reference.empty) {
                return std::nullopt;
            }
            if (reference.index >= resource.records.size()) {
                throw image_error("image reference is out of range");
            }
            const resource_record &target = resource.records[reference.index];
            if (target.type != "Image") {
                return std::nullopt;
            }
            return modern_image_source(engine_major, target, metadata_only);
        }

        std::optional<image_source> resolve(const binary_resource &resource, const image_limits &limits,
                                            const bool metadata_only = false)
        {
            const std::uint32_t engine_major = resource.engine.major;
            if (engine_major != engine_major_godot2 && engine_major != 3 && engine_major != 4) {
                return std::nullopt;
            }
            if (resource.records.empty()) {
                throw image_error("resource has no root record");
            }
            const resource_record &root = resource.records.back();

            // A root drawn from a supported image class must agree with the header's declared main
            // type; a mismatched pair is malformed rather than silently trusted. Any other class
            // resolves to no conversion regardless of the header.
            const bool known_image = supports_image_resource_class(root.type) &&
                                     (engine_major != engine_major_godot2 || root.type != "Image");
            if (!known_image) {
                return std::nullopt;
            }
            if (resource.main_type != root.type) {
                throw image_error("resource header type does not match the root record");
            }

            const std::optional<image_source> source =
                engine_major == engine_major_godot2 ? resolve_legacy(root, metadata_only)
                                                    : resolve_modern(engine_major, root, resource, metadata_only);
            if (source.has_value()) {
                if (source->raw) {
                    check_raw_budget(source->width, source->height, limits);
                    if (!raw_base_convertible(*source->descriptor, source->width, source->height,
                                              source->top_mip_bytes)) {
                        return std::nullopt;
                    }
                } else {
                    check_preserved_budget(source->preserved_bytes, limits);
                }
            }
            return source;
        }

        std::optional<image_resource_info> inspect(const binary_resource &resource, const image_limits &limits,
                                                   const bool metadata_only)
        {
            const auto source = resolve(resource, limits, metadata_only);
            if (!source.has_value()) {
                return std::nullopt;
            }
            if (source->raw) {
                // resolve already checked this geometry against 64-bit and caller-provided byte bounds.
                const std::uint64_t decoded =
                    static_cast<std::uint64_t>(source->width) * source->height * rgba_channels;
                return image_resource_info{source->extension, std::nullopt, decoded};
            }
            return image_resource_info{source->extension, source->preserved_bytes, 0};
        }
    } // namespace

    bool supports_raw_image(const std::uint32_t engine_major, const std::uint32_t wire_format) noexcept
    {
        return lookup_wire(engine_major, wire_format) != nullptr;
    }

    void write_raw_image(const std::span<const std::byte> top_mip, const std::uint32_t engine_major,
                         const std::uint32_t wire_format, const std::uint32_t width, const std::uint32_t height,
                         const std::function<void(std::span<const std::byte>)> &sink, const image_limits &limits)
    {
        const io::byte_source source{
            top_mip.size(), [&top_mip](const std::uint64_t offset, const std::span<std::byte> bytes) {
                std::copy_n(top_mip.data() + static_cast<std::size_t>(offset), bytes.size(), bytes.data());
            }};
        write_raw_image_streaming(source, engine_major, wire_format, width, height, sink, limits);
    }

    std::optional<image_resource_info> inspect_image_resource(const binary_resource &resource,
                                                              const image_limits &limits)
    {
        return inspect(resource, limits, false);
    }

    std::optional<image_resource_info> inspect_image_resource_metadata(const binary_resource &resource,
                                                                       const image_limits &limits)
    {
        return inspect(resource, limits, true);
    }

    std::optional<std::string_view> image_resource_extension(const binary_resource &resource,
                                                             const image_limits &limits)
    {
        const auto info = inspect_image_resource(resource, limits);
        if (!info.has_value()) {
            return std::nullopt;
        }
        return info->extension;
    }

    void write_image_resource(const binary_resource &resource,
                              const std::function<void(std::span<const std::byte>)> &sink, const image_limits &limits)
    {
        const std::optional<image_source> source = resolve(resource, limits);
        if (!source.has_value()) {
            throw image_error("unsupported image resource");
        }
        if (source->raw) {
            write_raw_image(source->top_mip, source->engine_major, source->wire_format, source->width, source->height,
                            sink, limits);
        } else {
            sink(source->preserved);
        }
    }
    void write_raw_image_streaming(const io::byte_source &top_mip, const std::uint32_t engine_major,
                                   const std::uint32_t wire_format, const std::uint32_t width,
                                   const std::uint32_t height, const io::byte_sink &sink, const image_limits &limits)
    {
        const format_descriptor *const descriptor = lookup_wire(engine_major, wire_format);
        if (descriptor == nullptr) {
            throw image_error("unsupported raw image format");
        }
        if (width == 0 || height == 0) {
            throw image_error("image dimensions must be non-zero");
        }
        check_raw_budget(width, height, limits);
        if (!raw_base_convertible(*descriptor, width, height, top_mip.size)) {
            throw image_error("raw image length does not match the format");
        }
        const std::uint64_t stored_row_bytes = descriptor->block_compressed
                                                   ? level_bytes(width, 4, *descriptor)
                                                   : static_cast<std::uint64_t>(width) * descriptor->element_bytes;
        if constexpr (sizeof(std::size_t) < sizeof(std::uint64_t)) {
            if (stored_row_bytes > (std::numeric_limits<std::size_t>::max)()) {
                throw image_error("image source row is not addressable");
            }
        }
        std::vector<std::byte> stored_row(static_cast<std::size_t>(stored_row_bytes));
        rgba_image decoded_strip;
        std::uint32_t cached_strip = (std::numeric_limits<std::uint32_t>::max)();
        codecs::encode_png_rows(
            width, height,
            [&](const std::uint32_t row, const std::span<std::byte> rgba) {
                if (descriptor->block_compressed) {
                    const std::uint32_t strip = row / 4;
                    if (strip != cached_strip) {
                        top_mip.read(static_cast<std::uint64_t>(strip) * stored_row_bytes, stored_row);
                        const std::uint32_t rows = (std::min)(std::uint32_t{4}, height - strip * 4);
                        decoded_strip = codecs::decode_bc(stored_row, descriptor->bc, width, rows, limits);
                        cached_strip = strip;
                    }
                    const std::size_t offset = static_cast<std::size_t>(row % 4) * rgba.size();
                    std::copy_n(decoded_strip.pixels.data() + offset, rgba.size(), rgba.data());
                    if (descriptor->alpha_to_green) {
                        // Godot's RA->RG conversion preserves R, copies A into G and expands RG8.
                        for (std::size_t index = 0; index < rgba.size(); index += 4) {
                            rgba[index + 1] = rgba[index + 3];
                            rgba[index + 2] = std::byte{0};
                            rgba[index + 3] = std::byte{0xFF};
                        }
                    }
                } else {
                    top_mip.read(static_cast<std::uint64_t>(row) * stored_row_bytes, stored_row);
                    for (std::uint32_t column = 0; column < width; ++column) {
                        std::uint8_t quad[4]{};
                        descriptor->read(
                            stored_row.data() + static_cast<std::size_t>(column) * descriptor->element_bytes, quad);
                        for (std::size_t channel = 0; channel < 4; ++channel) {
                            rgba[static_cast<std::size_t>(column) * 4 + channel] =
                                static_cast<std::byte>(quad[channel]);
                        }
                    }
                }
            },
            sink, limits);
    }

    void write_image_resource_streaming(const binary_resource &metadata, const io::byte_source &logical,
                                        const io::byte_sink &sink, const image_limits &limits)
    {
        const auto source = resolve(metadata, limits, true);
        if (!source.has_value()) {
            throw image_error("unsupported image resource");
        }
        if (source->media_offset > logical.size || source->media_bytes > logical.size - source->media_offset) {
            throw image_error("image payload exceeds its logical source");
        }
        if (!source->raw) {
            io::copy_byte_range(logical, source->media_offset + source->media_skip, source->preserved_bytes, sink);
            return;
        }
        const io::byte_source base{source->top_mip_bytes,
                                   [&](const std::uint64_t offset, const std::span<std::byte> bytes) {
                                       logical.read(source->media_offset + offset, bytes);
                                   }};
        write_raw_image_streaming(base, source->engine_major, source->wire_format, source->width, source->height, sink,
                                  limits);
    }
    bool supports_image_resource_class(const std::string_view class_name) noexcept
    {
        return class_name == "Image" || class_name == "ImageTexture";
    }
} // namespace unpackrat::godot
