#ifndef UNPACKRAT_TESTS_SUPPORT_GODOT_TEXTURE_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_GODOT_TEXTURE_FIXTURES_H

// Repository-owned fixtures for Godot texture envelopes (GDST / GST2). Every byte is generated
// here; no engine artefact or image payload from an archive is read.
//
// Layouts mirror the pinned official sources:
//   * 3.6-stable/scene/resources/texture.cpp        -> GDST "StreamTexture" header + PNG/WebP/raw body.
//   * 4.5-stable/scene/resources/compressed_texture.cpp -> GST2 header + ImageMetadata + body.
//
// Header geometry (little-endian):
//   GDST: magic[4] "GDST", width u16, custom-width u16, height u16, custom-height u16,
//         flags u32, data-format u32, then either a raw block or (PNG/WebP) a mip count u32
//         followed by a length u32 + bytes per mip level.
//   GST2: magic[4] "GST2", version u32, display-width u32, display-height u32, data-format u32,
//         mipmap-limit u32, reserved u32 x3, then ImageMetadata: data-format u32, width u16,
//         height u16, mipmaps u32, format u32, then the same block/raw body as GDST.
//
// The builders expose the raw header fields so both well-formed and deliberately malformed
// envelopes can be produced without ad-hoc byte surgery in each test.

#include "godot_fixtures.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <istream>
#include <optional>
#include <streambuf>
#include <string>
#include <vector>

namespace test::support
{
    // Reuse the Godot byte-vector alias and little-endian patch helpers from godot_fixtures.h.
    using godot_bytes = std::vector<std::uint8_t>;

    // --- Envelope magics -------------------------------------------------------------------
    inline constexpr char gdst_magic[5] = "GDST"; // Godot 3 streamable texture
    inline constexpr char gst2_magic[5] = "GST2"; // Godot 4 compressed texture

    // --- GDST data-format bits (3.6 StreamTexture::FormatBits) -----------------------------
    inline constexpr std::uint32_t gdst_format_mask = (1U << 20U) - 1U;
    inline constexpr std::uint32_t gdst_bit_png = 1U << 20U;
    inline constexpr std::uint32_t gdst_bit_webp = 1U << 21U;
    inline constexpr std::uint32_t gdst_bit_stream = 1U << 22U;
    inline constexpr std::uint32_t gdst_bit_mipmaps = 1U << 23U;

    // --- GST2 image data-format selector (4.5 CompressedTexture2D::DataFormat) --------------
    inline constexpr std::uint32_t gst2_data_image = 0;
    inline constexpr std::uint32_t gst2_data_png = 1;
    inline constexpr std::uint32_t gst2_data_webp = 2;
    inline constexpr std::uint32_t gst2_data_basis = 3;

    // --- Godot Image::Format ordinals shared by 3.6 core/image.h and 4.5 core/io/image.h ----
    inline constexpr std::uint32_t image_format_l8 = 0;
    inline constexpr std::uint32_t image_format_la8 = 1;
    inline constexpr std::uint32_t image_format_r8 = 2;
    inline constexpr std::uint32_t image_format_rg8 = 3;
    inline constexpr std::uint32_t image_format_rgb8 = 4;
    inline constexpr std::uint32_t image_format_rgba8 = 5;
    inline constexpr std::uint32_t image_format_rgba4444 = 6;    // not in the supported family set
    inline constexpr std::uint32_t image_format_dxt1 = 17;       // BC1
    inline constexpr std::uint32_t image_format_dxt3 = 18;       // BC2
    inline constexpr std::uint32_t image_format_dxt5 = 19;       // BC3
    inline constexpr std::uint32_t image_format_rgtc_r = 20;     // BC4
    inline constexpr std::uint32_t image_format_rgtc_rg = 21;    // BC5
    inline constexpr std::uint32_t image_format_bptc_rgba = 22;  // BC7 (LDR)
    inline constexpr std::uint32_t image_format_bptc_rgbf = 23;  // BC6H signed -- HDR, unsupported
    inline constexpr std::uint32_t image_format_bptc_rgbfu = 24; // BC6H unsigned -- HDR, unsupported
    inline constexpr std::uint32_t image_format_astc_4x4 = 35;   // ASTC -- unsupported (4.5 ordinal)

    // --- GDST (Godot 3) envelope -----------------------------------------------------------
    struct gdst_fixture final
    {
        std::string magic = gdst_magic;
        std::uint16_t width = 1;
        std::uint16_t custom_width = 0;
        std::uint16_t height = 1;
        std::uint16_t custom_height = 0;
        std::uint32_t flags = 0;
        std::uint32_t data_format = 0;
        // PNG/WebP: declared mip count, defaulting to the number of supplied payloads.
        std::optional<std::uint32_t> mipmap_count;
        std::vector<godot_bytes> mips;
        // Raw families: the contiguous stored image block (format = data_format & gdst_format_mask).
        std::optional<godot_bytes> raw_data;
        // Truncate the produced image to this many bytes to exercise range/table faults.
        std::optional<std::size_t> truncate_to;
    };

    [[nodiscard]] godot_bytes build_gdst(const gdst_fixture &fixture);

    // --- GST2 (Godot 4) envelope -----------------------------------------------------------
    struct gst2_fixture final
    {
        std::string magic = gst2_magic;
        std::uint32_t version = 1;
        std::optional<std::uint16_t> scale_tag; // extra field in ScaledCompressedTexture2D
        std::uint32_t display_width = 1;
        std::uint32_t display_height = 1;
        std::uint32_t data_format = 0;
        std::uint32_t mipmap_limit = 0;
        std::array<std::uint32_t, 3> reserved{0, 0, 0};
        std::uint32_t image_data_format = gst2_data_png;
        std::uint16_t image_width = 1;
        std::uint16_t image_height = 1;
        std::optional<std::uint32_t> image_mipmaps; // additional levels; default = mips.size() - 1
        std::uint32_t image_format = 0;
        std::vector<godot_bytes> mips;
        std::optional<godot_bytes> raw_data;
        std::optional<std::size_t> truncate_to;
    };

    [[nodiscard]] godot_bytes build_gst2(const gst2_fixture &fixture);

    // --- Small payload helpers --------------------------------------------------------------
    // A byte vector of exactly ``total`` bytes (raised to the 8-byte signature when shorter),
    // beginning with the PNG signature.
    [[nodiscard]] godot_bytes png_bytes(std::size_t total);

    // A standard RIFF/WEBP container payload ("RIFF" <size> "WEBP" "VP8L" ...) whose RIFF size
    // field matches its length, i.e. total - 8.
    [[nodiscard]] godot_bytes webp_riff_payload();

    // --- Expected-size helper ---------------------------------------------------------------
    // Bytes occupied by one stored level of the given Image::Format at width x height, using the
    // official pixel/block geometry (0 when the format is outside the supported set).
    [[nodiscard]] std::uint64_t godot_level_bytes(std::uint32_t format, std::uint32_t width, std::uint32_t height);

    // A std::istream over an in-memory buffer that counts the bytes actually delivered to the
    // reader, so a test can prove probing does not consume the whole encoded body.
    class counting_streambuf final : public std::streambuf
    {
      public:
        explicit counting_streambuf(std::string data);

        [[nodiscard]] std::uint64_t bytes_read() const noexcept;

      protected:
        int_type underflow() override;
        int_type uflow() override;
        std::streamsize xsgetn(char_type *target, std::streamsize count) override;
        pos_type seekoff(off_type offset, std::ios_base::seekdir direction, std::ios_base::openmode mode) override;
        pos_type seekpos(pos_type position, std::ios_base::openmode mode) override;

      private:
        std::string data_;
        std::size_t position_ = 0;
        std::uint64_t bytes_read_ = 0;
    };

    class counting_stream final : public std::istream
    {
      public:
        explicit counting_stream(std::string data);

        [[nodiscard]] std::uint64_t bytes_read() const noexcept;

      private:
        counting_streambuf buffer_;
    };

    // A std::istream that reports a nominal size but never delivers bytes, so a bounded read
    // fails and the prober must translate the stream fault into a project error.
    class unreadable_streambuf final : public std::streambuf
    {
      public:
        explicit unreadable_streambuf(std::streamoff nominal_size);

      protected:
        int_type underflow() override;
        int_type uflow() override;
        std::streamsize xsgetn(char_type *, std::streamsize) override;
        pos_type seekoff(off_type offset, std::ios_base::seekdir direction, std::ios_base::openmode mode) override;
        pos_type seekpos(pos_type position, std::ios_base::openmode mode) override;

      private:
        std::streamoff size_;
        std::streamoff position_ = 0;
    };

    class unreadable_stream final : public std::istream
    {
      public:
        explicit unreadable_stream(std::streamoff nominal_size);

      private:
        unreadable_streambuf buffer_;
    };
} // namespace test::support
#endif
