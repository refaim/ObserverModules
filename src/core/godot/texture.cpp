#include "texture.h"

#include "../io/bounded_stream.h"

#include <array>
#include <cstddef>
#include <limits>

namespace unpackrat::godot
{
    namespace
    {
        // GDST (Godot 3 StreamTexture) envelope geometry.
        constexpr std::size_t gdst_header_bytes = 20;
        constexpr std::uint32_t gdst_format_mask = (1U << 20U) - 1U;
        constexpr std::uint32_t gdst_bit_png = 1U << 20U;
        constexpr std::uint32_t gdst_bit_webp = 1U << 21U;
        constexpr std::uint32_t gdst_bit_mipmaps = 1U << 23U;

        // GST2 (Godot 4 CompressedTexture2D) envelope geometry.
        constexpr std::size_t gst2_header_bytes = 36;
        constexpr std::size_t gst2_metadata_bytes = 16;
        constexpr std::uint32_t gst2_format_version = 1;
        constexpr std::uint32_t gst2_data_image = 0;
        constexpr std::uint32_t gst2_data_png = 1;
        constexpr std::uint32_t gst2_data_webp = 2;
        constexpr std::uint32_t gst2_data_basis = 3;

        // Image::Format ordinals of the supported families. L8..BPTC_RGBFU share the same
        // ordinals in 3.6 core/image.h and 4.5 core/io/image.h, so the wire value maps directly.
        constexpr std::uint32_t format_l8 = 0;
        constexpr std::uint32_t format_la8 = 1;
        constexpr std::uint32_t format_r8 = 2;
        constexpr std::uint32_t format_rg8 = 3;
        constexpr std::uint32_t format_rgb8 = 4;
        constexpr std::uint32_t format_rgba8 = 5;
        constexpr std::uint32_t format_dxt1 = 17;
        constexpr std::uint32_t format_dxt3 = 18;
        constexpr std::uint32_t format_dxt5 = 19;
        constexpr std::uint32_t format_rgtc_r = 20;
        constexpr std::uint32_t format_rgtc_rg = 21;
        constexpr std::uint32_t format_bptc_rgba = 22;
        constexpr std::uint32_t format_dxt5_ra_as_rg = 34;

        // Encoded payload signatures.
        constexpr std::size_t png_signature_bytes = 8;
        constexpr char png_signature[png_signature_bytes] = {static_cast<char>(0x89),
                                                             'P',
                                                             'N',
                                                             'G',
                                                             static_cast<char>(0x0D),
                                                             static_cast<char>(0x0A),
                                                             static_cast<char>(0x1A),
                                                             static_cast<char>(0x0A)};
        constexpr std::size_t webp_wrapper_bytes = 4;
        constexpr std::size_t webp_riff_header_bytes = 12;

        [[nodiscard]] bool byte_equal(const char *left, const char *right, const std::size_t count) noexcept
        {
            for (std::size_t index = 0; index < count; ++index) {
                if (left[index] != right[index]) {
                    return false;
                }
            }
            return true;
        }

        [[nodiscard]] std::uint16_t load_u16(const char *bytes) noexcept
        {
            return static_cast<std::uint16_t>(
                static_cast<std::uint16_t>(static_cast<std::uint8_t>(bytes[0])) |
                static_cast<std::uint16_t>(static_cast<std::uint16_t>(static_cast<std::uint8_t>(bytes[1])) << 8U));
        }

        [[nodiscard]] std::uint32_t load_u32(const char *bytes) noexcept
        {
            return static_cast<std::uint32_t>(static_cast<std::uint8_t>(bytes[0])) |
                   static_cast<std::uint32_t>(static_cast<std::uint32_t>(static_cast<std::uint8_t>(bytes[1])) << 8U) |
                   static_cast<std::uint32_t>(static_cast<std::uint32_t>(static_cast<std::uint8_t>(bytes[2])) << 16U) |
                   static_cast<std::uint32_t>(static_cast<std::uint32_t>(static_cast<std::uint8_t>(bytes[3])) << 24U);
        }

        // Stored geometry of one Image::Format level: block size and bytes per block.
        struct pixel_geometry final
        {
            std::uint32_t block_width = 1;
            std::uint32_t block_height = 1;
            std::uint32_t bytes_per_block = 0;
            // True for the block-compressed families, whose stored size rounds each dimension up
            // to a whole block instead of multiplying pixels by bytes-per-pixel.
            bool compressed = false;
        };

        [[nodiscard]] bool pixel_geometry_for(const std::uint32_t engine_major, const std::uint32_t format,
                                              pixel_geometry &geometry) noexcept
        {
            switch (format) {
            case format_l8:
            case format_r8:
                geometry = pixel_geometry{1, 1, 1};
                return true;
            case format_la8:
            case format_rg8:
                geometry = pixel_geometry{1, 1, 2};
                return true;
            case format_rgb8:
                geometry = pixel_geometry{1, 1, 3};
                return true;
            case format_rgba8:
                geometry = pixel_geometry{1, 1, 4};
                return true;
            case format_dxt1:
            case format_rgtc_r:
                geometry = pixel_geometry{4, 4, 8, true};
                return true;
            case format_dxt3:
            case format_dxt5:
            case format_rgtc_rg:
            case format_bptc_rgba:
                geometry = pixel_geometry{4, 4, 16, true};
                return true;
            case format_dxt5_ra_as_rg:
                if (engine_major != 4) {
                    return false;
                }
                geometry = pixel_geometry{4, 4, 16, true};
                return true;
            default:
                return false;
            }
        }

        // Bytes occupied by one stored level of ``width`` x ``height``. The wire dimensions
        // are 16-bit and each stored block uses at most 16 bytes, so the product fits 64 bits.
        [[nodiscard]] std::uint64_t level_bytes(const pixel_geometry &geometry, const std::uint64_t width,
                                                const std::uint64_t height) noexcept
        {
            if (!geometry.compressed) {
                return width * height * geometry.bytes_per_block;
            }
            const std::uint64_t blocks_x = (width + geometry.block_width - 1) / geometry.block_width;
            const std::uint64_t blocks_y = (height + geometry.block_height - 1) / geometry.block_height;
            return blocks_x * blocks_y * geometry.bytes_per_block;
        }

        // Number of levels in the canonical Godot mip pyramid of ``width`` x ``height``: the
        // stored dimensions halve toward 1x1 (floored, never below one) for both plain and
        // block-compressed formats, so a 1x1 image carries exactly one level. A declared level
        // count above this is geometrically impossible and is rejected.
        [[nodiscard]] std::uint64_t max_mip_levels(const std::uint64_t width, const std::uint64_t height) noexcept
        {
            std::uint64_t levels = 1;
            std::uint64_t level_width = width;
            std::uint64_t level_height = height;
            while (level_width > 1 || level_height > 1) {
                level_width = level_width > 1 ? level_width / 2 : 1;
                level_height = level_height > 1 ? level_height / 2 : 1;
                ++levels;
            }
            return levels;
        }

        // Number of levels the stored raw chain actually occupies: the whole canonical pyramid when
        // the envelope requests mipmaps (Godot sizes a raw payload with Image::get_image_data_size),
        // otherwise the single base level. This EFFECTIVE count, not a declared field, is what the
        // caller's mip budget must admit, so a base-only image still needs a budget of at least one.
        [[nodiscard]] std::uint64_t effective_level_count(const bool full_chain, const std::uint64_t width,
                                                          const std::uint64_t height) noexcept
        {
            return full_chain ? max_mip_levels(width, height) : 1;
        }

        // Stored dimension of mip level ``level`` (0 = base): halved ``level`` times and never
        // below one.
        [[nodiscard]] std::uint64_t level_dimension(const std::uint64_t size, const std::uint64_t level) noexcept
        {
            const std::uint64_t halved = size >> level;
            return halved > 0 ? halved : 1;
        }

        // Bytes occupied by the full required mip chain of ``width`` x ``height``. Godot sizes a
        // raw payload with Image::get_image_data_size, which sums the pyramid down to 1x1.
        [[nodiscard]] std::uint64_t chain_bytes(const pixel_geometry &geometry, const std::uint64_t width,
                                                const std::uint64_t height) noexcept
        {
            std::uint64_t total = 0;
            const std::uint64_t levels = max_mip_levels(width, height);
            for (std::uint64_t level = 0; level < levels; ++level) {
                total += level_bytes(geometry, level_dimension(width, level), level_dimension(height, level));
            }
            return total;
        }

        // A strictly bounded absolute window over the underlying stream. Every read validates
        // that it stays inside ``[begin, end)`` before it seeks, so no enclosing envelope can
        // step outside its own source range.
        class window_reader final
        {
          public:
            window_reader(std::istream &stream, const std::uint64_t begin, const std::uint64_t end)
                : stream_(stream), begin_(begin), end_(end)
            {
                // A window that reaches past the bytes the stream actually holds is a source
                // fault (the archive shrank between listing and this probe), not a malformed
                // envelope, so it is reported as the texture_source_error subtype.
                if (end_ > static_cast<std::uint64_t>(stream_.size())) {
                    throw texture_source_error("texture source range extends past the stream");
                }
            }

            [[nodiscard]] std::uint64_t begin() const noexcept
            {
                return begin_;
            }

            [[nodiscard]] std::uint64_t end() const noexcept
            {
                return end_;
            }

            void read(const std::uint64_t offset, char *destination, const std::size_t count)
            {
                // A single containment test for both ends: an offset below the window base wraps
                // the subtraction to a value beyond the window span, exactly like an offset past
                // the window end, and the trailing byte count is then bounded against what is left.
                const std::uint64_t window = end_ - begin_;
                const std::uint64_t position = offset - begin_;
                if (position > window || count > window - position) {
                    throw texture_error("texture envelope read is out of range");
                }
                stream_.seek_absolute(static_cast<std::streamoff>(offset));
                stream_.read_exact(destination, count);
            }

            [[nodiscard]] std::uint32_t u32(const std::uint64_t offset)
            {
                std::array<char, 4> bytes{};
                read(offset, bytes.data(), bytes.size());
                return load_u32(bytes.data());
            }

          private:
            io::bounded_stream stream_;
            std::uint64_t begin_;
            std::uint64_t end_;
        };

        void validate_dimensions(const std::uint32_t width, const std::uint32_t height, const texture_limits &limits)
        {
            if (width == 0 || height == 0) {
                throw texture_error("texture has a zero dimension");
            }
            if (static_cast<std::uint64_t>(width) * height > limits.max_pixels) {
                throw texture_error("texture exceeds the pixel budget");
            }
        }

        // Validates an encoded payload and returns the range to expose. A PNG body is preserved
        // verbatim after its signature check; a WebP body has only the optional Godot 3 wrapper
        // removed, and the standard RIFF/WebP header is checked without touching the bitstream.
        [[nodiscard]] byte_range validate_encoded_body(window_reader &reader, const texture_encoding encoding,
                                                       const bool wrapped, const std::uint64_t offset,
                                                       const std::uint64_t length)
        {
            if (encoding == texture_encoding::png) {
                if (length < png_signature_bytes) {
                    throw texture_error("png payload is shorter than its signature");
                }
                std::array<char, png_signature_bytes> signature{};
                reader.read(offset, signature.data(), signature.size());
                if (!byte_equal(signature.data(), png_signature, png_signature_bytes)) {
                    throw texture_error("png payload signature mismatch");
                }
                return byte_range{offset, length};
            }

            std::uint64_t riff_offset = offset;
            if (wrapped) {
                if (length < webp_wrapper_bytes) {
                    throw texture_error("webp payload is shorter than the wrapper");
                }
                std::array<char, webp_wrapper_bytes> wrapper{};
                reader.read(offset, wrapper.data(), wrapper.size());
                if (!byte_equal(wrapper.data(), "WEBP", webp_wrapper_bytes)) {
                    throw texture_error("webp wrapper is missing");
                }
                riff_offset = offset + webp_wrapper_bytes;
            }

            const std::uint64_t riff_length = length - (riff_offset - offset);
            if (riff_length < webp_riff_header_bytes) {
                throw texture_error("webp payload is shorter than a RIFF container");
            }
            std::array<char, webp_riff_header_bytes> header{};
            reader.read(riff_offset, header.data(), header.size());
            if (!byte_equal(header.data(), "RIFF", 4) || !byte_equal(header.data() + 8, "WEBP", 4)) {
                throw texture_error("webp payload is not a RIFF/WebP container");
            }
            if (static_cast<std::uint64_t>(load_u32(header.data() + 4)) + 8 != riff_length) {
                throw texture_error("webp RIFF length does not match the stored payload");
            }
            return byte_range{riff_offset, riff_length};
        }

        [[nodiscard]] texture_payload probe_gdst(window_reader &reader, const texture_limits &limits)
        {
            const std::uint64_t base = reader.begin();
            std::array<char, gdst_header_bytes> header{};
            reader.read(base, header.data(), header.size());

            const std::uint32_t width = load_u16(header.data() + 4);
            const std::uint32_t height = load_u16(header.data() + 8);
            const std::uint32_t data_format = load_u32(header.data() + 16);
            const std::uint32_t format = data_format & gdst_format_mask;

            texture_payload payload;
            payload.engine_major = 3;
            payload.width = width;
            payload.height = height;
            payload.wire_image_format = format;

            // The header read above guarantees this stays inside the source window.
            const std::uint64_t header_end = base + gdst_header_bytes;

            if ((data_format & (gdst_bit_png | gdst_bit_webp)) != 0) {
                payload.encoding = (data_format & gdst_bit_png) != 0 ? texture_encoding::png : texture_encoding::webp;
                validate_dimensions(width, height, limits);

                const std::uint32_t count = reader.u32(header_end);
                if (count == 0) {
                    throw texture_error("texture declares no mip levels");
                }
                if (count > limits.max_mipmaps) {
                    throw texture_error("texture mip count exceeds the budget");
                }
                if (static_cast<std::uint64_t>(count) > max_mip_levels(width, height)) {
                    throw texture_error("texture mip count exceeds the image pyramid");
                }

                // Every declared level must be present and carry a valid fixed signature/header:
                // the 3.6 loader reads exactly the declared chain, so a missing or truncated tail
                // level is a structural fault, never a tolerated end of table. Only the top
                // level's range is exposed; the remaining levels are bounded and validated in
                // place without decoding or allocating their payloads.
                std::uint64_t cursor = header_end + 4;
                byte_range body{};
                for (std::uint32_t level = 0; level < count; ++level) {
                    const std::uint32_t length = reader.u32(cursor);
                    cursor += 4;
                    const byte_range level_body = validate_encoded_body(reader, payload.encoding, true, cursor, length);
                    if (level == 0) {
                        body = level_body;
                    }
                    cursor += length;
                }
                if (cursor > reader.end()) {
                    throw texture_error("texture mip table runs past the source");
                }
                payload.body = body;
                return payload;
            }

            pixel_geometry geometry{};
            if (!pixel_geometry_for(payload.engine_major, format, geometry)) {
                throw texture_unsupported("texture pixel format is not supported");
            }
            validate_dimensions(width, height, limits);

            // FORMAT_BIT_HAS_MIPMAPS makes the 3.6 loader size the stored block as the full
            // pyramid down to 1x1, so the whole chain must be present even though only the top
            // level is exposed. Without the bit only the top level is stored. The effective level
            // count is what the caller's mip budget must admit, exactly like the encoded path.
            const bool has_mips = (data_format & gdst_bit_mipmaps) != 0;
            if (effective_level_count(has_mips, width, height) > limits.max_mipmaps) {
                throw texture_error("texture mip count exceeds the budget");
            }
            const std::uint64_t required =
                has_mips ? chain_bytes(geometry, width, height) : level_bytes(geometry, width, height);
            if (required > reader.end() - header_end) {
                throw texture_error("texture raw payload runs past the source");
            }
            payload.encoding = texture_encoding::raw;
            payload.body = byte_range{header_end, level_bytes(geometry, width, height)};
            return payload;
        }

        [[nodiscard]] texture_payload probe_gst2(window_reader &reader, const texture_limits &limits)
        {
            const std::uint64_t base = reader.begin();
            constexpr std::size_t prefix_bytes = gst2_header_bytes + gst2_metadata_bytes;
            constexpr std::size_t scale_bytes = 2;
            std::array<char, prefix_bytes + scale_bytes> header{};
            reader.read(base, header.data(), prefix_bytes);

            const std::uint32_t version = load_u32(header.data() + 4);
            if (version > gst2_format_version) {
                throw texture_error("texture version is newer than supported");
            }
            std::size_t metadata_offset = gst2_header_bytes;
            // ScaledCompressedTexture2D inserts a uint16 scale field after the version. The
            // payload metadata still identifies PNG, WebP or raw pixels. Prefer canonical GST2
            // whenever its metadata is valid; probe the shifted layout only when it is not.
            const char *metadata = header.data() + metadata_offset;
            if (load_u32(metadata) > gst2_data_basis || load_u16(metadata + 4) == 0 || load_u16(metadata + 6) == 0) {
                if (reader.end() - base >= header.size()) {
                    reader.read(base + prefix_bytes, header.data() + prefix_bytes, scale_bytes);
                    const char *const scaled = metadata + scale_bytes;
                    if (load_u32(scaled) <= gst2_data_basis && load_u16(scaled + 4) != 0 && load_u16(scaled + 6) != 0) {
                        metadata_offset += scale_bytes;
                        metadata = scaled;
                    }
                }
            }
            const std::uint32_t data_format = load_u32(metadata);
            const std::uint32_t width = load_u16(metadata + 4);
            const std::uint32_t height = load_u16(metadata + 6);
            const std::uint32_t mipmaps = load_u32(metadata + 8);
            const std::uint32_t format = load_u32(metadata + 12);

            texture_payload payload;
            payload.engine_major = 4;
            payload.width = width;
            payload.height = height;
            payload.wire_image_format = format;

            // The header read above guarantees this stays inside the source window.
            const std::uint64_t metadata_end = base + metadata_offset + gst2_metadata_bytes;
            const std::uint64_t count = static_cast<std::uint64_t>(mipmaps) + 1;

            if (data_format == gst2_data_png || data_format == gst2_data_webp) {
                payload.encoding = data_format == gst2_data_png ? texture_encoding::png : texture_encoding::webp;
                validate_dimensions(width, height, limits);
                if (count > limits.max_mipmaps) {
                    throw texture_error("texture mip count exceeds the budget");
                }
                if (count > max_mip_levels(width, height)) {
                    throw texture_error("texture mip count exceeds the image pyramid");
                }

                // Bounded, in-place validation of every declared level: each level must be present
                // and begin with a valid fixed signature/header. Only the top level's range is
                // exposed, and no pixel payload is decoded or allocated.
                std::uint64_t cursor = metadata_end;
                byte_range body{};
                for (std::uint64_t level = 0; level < count; ++level) {
                    const std::uint32_t length = reader.u32(cursor);
                    cursor += 4;
                    const byte_range level_body =
                        validate_encoded_body(reader, payload.encoding, false, cursor, length);
                    if (level == 0) {
                        body = level_body;
                    }
                    cursor += length;
                }
                if (cursor > reader.end()) {
                    throw texture_error("texture mip table runs past the source");
                }
                payload.body = body;
                return payload;
            }

            if (data_format == gst2_data_basis) {
                throw texture_unsupported("texture uses Basis Universal, which is not supported");
            }
            if (data_format != gst2_data_image) {
                throw texture_unsupported("texture uses an unknown data format");
            }

            pixel_geometry geometry{};
            if (!pixel_geometry_for(payload.engine_major, format, geometry)) {
                throw texture_unsupported("texture pixel format is not supported");
            }
            validate_dimensions(width, height, limits);
            if (effective_level_count(mipmaps != 0, width, height) > limits.max_mipmaps) {
                throw texture_error("texture mip count exceeds the budget");
            }
            if (count > limits.max_mipmaps) {
                throw texture_error("texture mip count exceeds the budget");
            }
            if (count > max_mip_levels(width, height)) {
                throw texture_error("texture mip count exceeds the image pyramid");
            }

            // Godot reads the full required chain for a raw payload, so require every level of it
            // to be present while exposing only the top level.
            const std::uint64_t required =
                mipmaps != 0 ? chain_bytes(geometry, width, height) : level_bytes(geometry, width, height);
            if (required > reader.end() - metadata_end) {
                throw texture_error("texture raw payload runs past the source");
            }
            payload.encoding = texture_encoding::raw;
            payload.body = byte_range{metadata_end, level_bytes(geometry, width, height)};
            return payload;
        }
    } // namespace

    texture_payload probe_texture(std::istream &stream, const byte_range source, const texture_limits &limits)
    {
        if (source.size > limits.max_source_bytes) {
            throw texture_error("texture source exceeds the byte budget");
        }
        if (source.size > std::numeric_limits<std::uint64_t>::max() - source.offset) {
            throw texture_error("texture source range overflows");
        }
        const std::uint64_t end = source.offset + source.size;

        try {
            window_reader reader(stream, source.offset, end);
            std::array<char, 4> magic{};
            reader.read(source.offset, magic.data(), magic.size());
            if (byte_equal(magic.data(), "GDST", magic.size())) {
                return probe_gdst(reader, limits);
            }
            if (byte_equal(magic.data(), "GST2", magic.size())) {
                return probe_gst2(reader, limits);
            }
            throw texture_error("texture magic is not recognized");
        } catch (const io::read_error &) {
            // The bounded reader failed to seek or deliver a requested byte range. That is a
            // source-stream fault, not a malformed envelope, so it is reported as the
            // texture_source_error subtype (a texture_error by inheritance).
            throw texture_source_error("texture stream is not readable");
        }
    }
} // namespace unpackrat::godot
