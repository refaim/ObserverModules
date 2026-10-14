// Focused behavior tests for the Godot texture-envelope prober.
//
// Contract under test: unpackrat::godot::probe_texture(std::istream&, byte_range source,
// const texture_limits& = {}) -> texture_payload. The probe reads only the fixed envelope
// headers (and, for compressed payloads, a small fixed signature); it never decodes an image or
// allocates an image buffer. The returned body is the ABSOLUTE top-mip payload range within
// ``source``.
//
// Every fixture is repository-owned and generated in-memory; the expected byte geometry comes
// from the pinned official sources (3.6 scene/resources/texture.{h,cpp}, 4.5
// scene/resources/compressed_texture.{h,cpp}, the 3.6 modules/webp/image_loader_webp.cpp "WEBP"
// wrapper and the shared Image::Format ordinals).
//
// Two resolved contract points: ``wire_image_format`` is the Godot Image::Format ordinal carried
// on the wire (the low 20 bits of GDST's data-format word, or GST2's ImageMetadata ``format``
// field); limit breaches (source size, pixel count, mip count) and structural faults raise
// texture_error while formats outside the supported family raise texture_unsupported.

#include "../../core/godot/pck.h"
#include "../../core/godot/texture.h"
#include "../support/godot_texture_fixtures.h"

#include <cstddef>
#include <cstdint>
#include <ios>
#include <limits>
#include <sstream>
#include <streambuf>
#include <string>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using test::support::build_gdst;
    using test::support::build_gst2;
    using test::support::counting_stream;
    using test::support::gdst_fixture;
    using test::support::godot_bytes;
    using test::support::godot_level_bytes;
    using test::support::gst2_fixture;
    using test::support::patch_u32;
    using test::support::png_bytes;
    using test::support::unreadable_stream;
    using test::support::webp_riff_payload;

    using unpackrat::godot::byte_range;
    using unpackrat::godot::probe_texture;
    using unpackrat::godot::texture_encoding;
    using unpackrat::godot::texture_error;
    using unpackrat::godot::texture_limits;
    using unpackrat::godot::texture_payload;
    using unpackrat::godot::texture_source_error;
    using unpackrat::godot::texture_unsupported;

    // Header geometry (little-endian), derived from the pinned official sources.
    constexpr std::uint64_t gdst_header_bytes = 20;
    constexpr std::uint64_t gdst_body_offset = gdst_header_bytes + 8; // + mip count + first length
    constexpr std::uint64_t gst2_prefix_bytes = 52;                   // 36-byte header + 16-byte metadata
    constexpr std::uint64_t gst2_body_offset = gst2_prefix_bytes + 4; // + first length

    std::string as_text(const godot_bytes &bytes)
    {
        return std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    }

    texture_payload probe(const godot_bytes &image, const byte_range source, const texture_limits &limits = {})
    {
        std::istringstream stream(as_text(image));
        return probe_texture(stream, source, limits);
    }

    byte_range whole(const godot_bytes &bytes)
    {
        return byte_range{0, bytes.size()};
    }

    std::vector<std::uint8_t> bytes_at(const godot_bytes &bytes, const byte_range range)
    {
        const auto begin = bytes.begin() + static_cast<std::ptrdiff_t>(range.offset);
        return std::vector<std::uint8_t>(begin, begin + static_cast<std::ptrdiff_t>(range.size));
    }

    godot_bytes offset_by(const std::size_t lead, const godot_bytes &image)
    {
        godot_bytes shifted(lead, 0xAB);
        shifted.insert(shifted.end(), image.begin(), image.end());
        return shifted;
    }

    godot_bytes wrap_webp(const godot_bytes &riff)
    {
        godot_bytes wrapped = {'W', 'E', 'B', 'P'};
        wrapped.insert(wrapped.end(), riff.begin(), riff.end());
        return wrapped;
    }

    // A std::streambuf whose reads and seeks both fail, so the prober cannot even establish the
    // physical extent of the source: a pure source-stream fault rather than a malformed envelope.
    class faulting_streambuf final : public std::streambuf
    {
      protected:
        int_type underflow() override
        {
            return traits_type::eof();
        }

        int_type uflow() override
        {
            return traits_type::eof();
        }

        std::streamsize xsgetn(char_type *, std::streamsize) override
        {
            return 0;
        }

        pos_type seekoff(off_type, std::ios_base::seekdir, std::ios_base::openmode) override
        {
            return pos_type(off_type(-1));
        }

        pos_type seekpos(pos_type, std::ios_base::openmode) override
        {
            return pos_type(off_type(-1));
        }
    };

    class faulting_stream final : public std::istream
    {
      public:
        faulting_stream() : std::istream(nullptr)
        {
            rdbuf(&buffer_);
            clear();
        }

      private:
        faulting_streambuf buffer_;
    };

    // Requires ``action`` to raise the ORDINARY envelope error: a base texture_error that is NOT
    // the source-fault subtype. It proves a malformed or truncated envelope inside a healthy
    // source window still maps to a declined conversion, never to a source read failure.
    template <typename Action> void check_envelope_error(Action &&action)
    {
        bool source_fault = false;
        bool envelope_fault = false;
        try {
            action();
        } catch (const texture_source_error &) {
            source_fault = true;
        } catch (const texture_error &) {
            envelope_fault = true;
        }
        CHECK(envelope_fault);
        CHECK(!(source_fault));
    }
} // namespace

TEST_CASE("godot texture: GDST PNG reports the absolute top-mip range", "[godot][texture]")
{
    const godot_bytes base = png_bytes(11);
    gdst_fixture fixture;
    fixture.width = 8;
    fixture.custom_width = 99;
    fixture.height = 8;
    fixture.custom_height = 77;
    fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
    fixture.mips = {base, png_bytes(3)};
    const godot_bytes image = build_gdst(fixture);

    SECTION("source anchored at zero")
    {
        const texture_payload payload = probe(image, whole(image));
        CHECK(payload.encoding == texture_encoding::png);
        CHECK(payload.engine_major == 3U);
        // Stored pixel dimensions, never the custom/display dimensions.
        CHECK(payload.width == 8U);
        CHECK(payload.height == 8U);
        CHECK(payload.wire_image_format == 0U);
        CHECK(payload.body.offset == gdst_body_offset);
        CHECK(payload.body.size == base.size());
        const auto body = bytes_at(image, payload.body);
        REQUIRE(body.size() == base.size());
        CHECK(body[0] == 0x89); // PNG signature of the top level
    }

    SECTION("nonzero source offset yields an absolute body range")
    {
        const std::size_t lead = 7;
        const godot_bytes shifted = offset_by(lead, image);
        const texture_payload payload = probe(shifted, byte_range{lead, image.size()});
        CHECK(payload.body.offset == lead + gdst_body_offset);
        CHECK(payload.body.size == base.size());
    }
}

TEST_CASE("godot texture: GST2 PNG reports stored pixel dimensions and the top-mip range", "[godot][texture]")
{
    const godot_bytes base = png_bytes(13);
    gst2_fixture fixture;
    fixture.display_width = 1024;
    fixture.display_height = 512;
    fixture.image_data_format = test::support::gst2_data_png;
    fixture.image_width = 8;
    fixture.image_height = 4;
    fixture.image_format = test::support::image_format_rgba8;
    fixture.mips = {base, png_bytes(2)};
    const godot_bytes image = build_gst2(fixture);

    SECTION("source anchored at zero")
    {
        const texture_payload payload = probe(image, whole(image));
        CHECK(payload.encoding == texture_encoding::png);
        CHECK(payload.engine_major == 4U);
        // Stored image dimensions win over the display dimensions.
        CHECK(payload.width == 8U);
        CHECK(payload.height == 4U);
        CHECK(payload.wire_image_format == test::support::image_format_rgba8);
        CHECK(payload.body.offset == gst2_body_offset);
        CHECK(payload.body.size == base.size());
    }

    SECTION("nonzero source offset yields an absolute body range")
    {
        const std::size_t lead = 40;
        const godot_bytes shifted = offset_by(lead, image);
        const texture_payload payload = probe(shifted, byte_range{lead, image.size()});
        CHECK(payload.body.offset == lead + gst2_body_offset);
        CHECK(payload.body.size == base.size());
    }
}

TEST_CASE("godot texture: GDST WebP strips only the extra WEBP wrapper", "[godot][texture]")
{
    const godot_bytes riff = webp_riff_payload();
    gdst_fixture fixture;
    fixture.width = 16;
    fixture.height = 16;
    fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
    fixture.mips = {wrap_webp(riff)};
    const godot_bytes image = build_gdst(fixture);

    const texture_payload payload = probe(image, whole(image));
    CHECK(payload.encoding == texture_encoding::webp);
    CHECK(payload.engine_major == 3U);
    CHECK(payload.body.offset == gdst_body_offset + 4); // exactly the four-byte prefix
    CHECK(payload.body.size == riff.size());
    CHECK(bytes_at(image, payload.body) == riff);
}

TEST_CASE("godot texture: GST2 WebP body is the standard RIFF payload", "[godot][texture]")
{
    const godot_bytes riff = webp_riff_payload();
    gst2_fixture fixture;
    fixture.display_width = 16;
    fixture.display_height = 16;
    fixture.image_data_format = test::support::gst2_data_webp;
    fixture.image_width = 16;
    fixture.image_height = 16;
    fixture.image_format = test::support::image_format_rgba8;
    fixture.mips = {riff};
    const godot_bytes image = build_gst2(fixture);

    const texture_payload payload = probe(image, whole(image));
    CHECK(payload.encoding == texture_encoding::webp);
    CHECK(payload.engine_major == 4U);
    CHECK(payload.body.offset == gst2_body_offset);
    CHECK(payload.body.size == riff.size());
    CHECK(bytes_at(image, payload.body) == riff);
}

TEST_CASE("godot texture: complete multi-mip chains expose the top level", "[godot][texture]")
{
    SECTION("GDST accepts a complete chain and reports the base level")
    {
        const godot_bytes base = png_bytes(11);
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format =
            test::support::gdst_bit_png | test::support::gdst_bit_stream | test::support::gdst_bit_mipmaps;
        fixture.mips = {base, png_bytes(6), png_bytes(3)};
        const godot_bytes image = build_gdst(fixture);

        const texture_payload payload = probe(image, whole(image));
        CHECK(payload.encoding == texture_encoding::png);
        CHECK(payload.body.offset == gdst_body_offset);
        CHECK(payload.body.size == base.size());
    }

    SECTION("GDST rejects a declared tail level that is entirely missing")
    {
        const godot_bytes base = png_bytes(11);
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format =
            test::support::gdst_bit_png | test::support::gdst_bit_stream | test::support::gdst_bit_mipmaps;
        fixture.mipmap_count = 3; // declares three levels
        fixture.mips = {base};    // but only the base level is stored
        const godot_bytes image = build_gdst(fixture);

        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST rejects a declared tail level whose payload is truncated")
    {
        const godot_bytes base = png_bytes(9);
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format =
            test::support::gdst_bit_png | test::support::gdst_bit_stream | test::support::gdst_bit_mipmaps;
        fixture.mips = {base, png_bytes(6)};
        // Keep the second level's length field but only two of its six declared bytes.
        fixture.truncate_to = static_cast<std::size_t>(gdst_body_offset + base.size() + 4 + 2);
        const godot_bytes image = build_gdst(fixture);

        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 accepts a complete chain and reports the base level")
    {
        const godot_bytes base = png_bytes(11);
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = {base, png_bytes(6), png_bytes(3)};
        const godot_bytes image = build_gst2(fixture);

        const texture_payload payload = probe(image, whole(image));
        CHECK(payload.body.offset == gst2_body_offset);
        CHECK(payload.body.size == base.size());
    }

    SECTION("GST2 rejects a short chain")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_mipmaps = 2;                    // declares three levels
        fixture.mips = {png_bytes(11), png_bytes(6)}; // but only two are stored
        const godot_bytes image = build_gst2(fixture);

        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }
}

TEST_CASE("godot texture: supported raw and block-compressed families are accepted", "[godot][texture]")
{
    struct raw_case final
    {
        std::uint32_t format;
        std::uint16_t width;
        std::uint16_t height;
    };

    const raw_case cases[] = {
        {test::support::image_format_l8, 8, 8},      {test::support::image_format_la8, 8, 8},
        {test::support::image_format_r8, 4, 4},      {test::support::image_format_rg8, 4, 4},
        {test::support::image_format_rgb8, 4, 4},    {test::support::image_format_rgba8, 4, 4},
        {test::support::image_format_dxt1, 4, 4},    {test::support::image_format_dxt3, 4, 4},
        {test::support::image_format_dxt5, 8, 8},    {test::support::image_format_rgtc_r, 4, 4},
        {test::support::image_format_rgtc_rg, 4, 4}, {test::support::image_format_bptc_rgba, 8, 8},
    };

    for (const raw_case &current : cases) {
        const auto expected = godot_level_bytes(current.format, current.width, current.height);
        REQUIRE(expected > 0U);
        const godot_bytes stored(static_cast<std::size_t>(expected), 0x5A);

        INFO("format ordinal " << current.format);

        gdst_fixture gdst;
        gdst.width = current.width;
        gdst.height = current.height;
        gdst.data_format = current.format;
        gdst.raw_data = stored;
        const godot_bytes gdst_image = build_gdst(gdst);

        const texture_payload gdst_payload = probe(gdst_image, whole(gdst_image));
        CHECK(gdst_payload.encoding == texture_encoding::raw);
        CHECK(gdst_payload.engine_major == 3U);
        CHECK(gdst_payload.wire_image_format == current.format);
        CHECK(gdst_payload.width == current.width);
        CHECK(gdst_payload.height == current.height);
        CHECK(gdst_payload.body.offset == gdst_header_bytes);
        CHECK(gdst_payload.body.size == expected);

        gst2_fixture gst2;
        gst2.image_data_format = test::support::gst2_data_image;
        gst2.image_width = current.width;
        gst2.image_height = current.height;
        gst2.image_format = current.format;
        gst2.raw_data = stored;
        const godot_bytes gst2_image = build_gst2(gst2);

        const texture_payload gst2_payload = probe(gst2_image, whole(gst2_image));
        CHECK(gst2_payload.encoding == texture_encoding::raw);
        CHECK(gst2_payload.engine_major == 4U);
        CHECK(gst2_payload.wire_image_format == current.format);
        CHECK(gst2_payload.body.offset == gst2_prefix_bytes);
        CHECK(gst2_payload.body.size == expected);
    }
}

TEST_CASE("godot texture: raw payloads validate the declared and required chains", "[godot][texture]")
{
    SECTION("GDST raw with the mipmaps bit requires the full stored pyramid")
    {
        // 4x4 RGBA8 stores 64 + 16 + 4 = 84 bytes once the mipmaps bit requests the pyramid.
        gdst_fixture truncated;
        truncated.width = 4;
        truncated.height = 4;
        truncated.data_format = test::support::image_format_rgba8 | test::support::gdst_bit_mipmaps;
        truncated.raw_data = godot_bytes(64, 0x5A); // top level only: one level short
        const godot_bytes truncated_image = build_gdst(truncated);
        CHECK_THROWS_AS(probe(truncated_image, whole(truncated_image)), texture_error);

        gdst_fixture complete;
        complete.width = 4;
        complete.height = 4;
        complete.data_format = test::support::image_format_rgba8 | test::support::gdst_bit_mipmaps;
        complete.raw_data = godot_bytes(64 + 16 + 4, 0x5A);
        const godot_bytes complete_image = build_gdst(complete);

        const texture_payload payload = probe(complete_image, whole(complete_image));
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.body.offset == gdst_header_bytes);
        CHECK(payload.body.size == 64U); // only the top level is exposed
    }

    SECTION("GST2 raw accepts a full required chain and reports the top level")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.image_mipmaps = 1;
        fixture.raw_data = godot_bytes(340, 0x5A); // 256 + 64 + 16 + 4
        const godot_bytes image = build_gst2(fixture);

        const texture_payload payload = probe(image, whole(image));
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.body.offset == gst2_prefix_bytes);
        CHECK(payload.body.size == 256U);
    }

    SECTION("GST2 raw rejects a chain that is shorter than the required one")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.image_mipmaps = 1;
        fixture.raw_data = godot_bytes(300, 0x5A);
        const godot_bytes image = build_gst2(fixture);

        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 raw rejects a mip count over the budget")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_width = 4;
        fixture.image_height = 4;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.image_mipmaps = 64;
        fixture.raw_data = godot_bytes(64, 0x5A);
        const godot_bytes image = build_gst2(fixture);

        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }
}

TEST_CASE("godot texture: raw pyramids follow the halving chain for rectangular images", "[godot][texture]")
{
    // 1x8 and 8x1 L8 pyramids both total 8 + 4 + 2 + 1 = 15 bytes.
    SECTION("GDST raw honours a rectangular chain sized by the halving rule")
    {
        gdst_fixture fixture;
        fixture.width = 1;
        fixture.height = 8;
        fixture.data_format = test::support::image_format_l8 | test::support::gdst_bit_mipmaps;
        fixture.raw_data = godot_bytes(14, 0x11); // one byte short of the 15-byte chain
        const godot_bytes short_image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(short_image, whole(short_image)), texture_error);

        fixture.raw_data = godot_bytes(15, 0x11);
        const godot_bytes complete_image = build_gdst(fixture);
        const texture_payload payload = probe(complete_image, whole(complete_image));
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.body.offset == gdst_header_bytes);
        CHECK(payload.body.size == 8U); // the 1x8 top level
    }

    SECTION("GST2 raw honours a rectangular chain sized by the halving rule")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_width = 8;
        fixture.image_height = 1;
        fixture.image_format = test::support::image_format_l8;
        fixture.image_mipmaps = 1;
        fixture.raw_data = godot_bytes(15, 0x11);
        const godot_bytes image = build_gst2(fixture);

        const texture_payload payload = probe(image, whole(image));
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.body.offset == gst2_prefix_bytes);
        CHECK(payload.body.size == 8U); // the 8x1 top level
    }
}

TEST_CASE("godot texture: raw mip chains respect the configured mip budget", "[godot][texture]")
{
    // A mipmapped raw payload occupies the whole canonical pyramid, so the budget must admit its
    // EFFECTIVE level count rather than a declared field; a base-only payload still needs one.
    SECTION("GDST raw pyramid at the exact budget succeeds and one level below fails")
    {
        gdst_fixture fixture;
        fixture.width = 4;
        fixture.height = 4;
        fixture.data_format = test::support::image_format_rgba8 | test::support::gdst_bit_mipmaps;
        fixture.raw_data = godot_bytes(64 + 16 + 4, 0x5A); // 4x4 RGBA8 pyramid: three levels
        const godot_bytes image = build_gdst(fixture);

        texture_limits exact;
        exact.max_mipmaps = 3;
        const texture_payload payload = probe(image, whole(image), exact);
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.body.size == 64U);

        texture_limits tight;
        tight.max_mipmaps = 2;
        CHECK_THROWS_AS(probe(image, whole(image), tight), texture_error);
    }

    SECTION("GST2 raw pyramid at the exact budget succeeds and one level below fails")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.image_mipmaps = 1;
        fixture.raw_data = godot_bytes(256 + 64 + 16 + 4, 0x5A); // 8x8 RGBA8 pyramid: four levels
        const godot_bytes image = build_gst2(fixture);

        texture_limits exact;
        exact.max_mipmaps = 4;
        const texture_payload payload = probe(image, whole(image), exact);
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.body.size == 256U);

        texture_limits tight;
        tight.max_mipmaps = 3;
        CHECK_THROWS_AS(probe(image, whole(image), tight), texture_error);
    }

    SECTION("A base-only raw level still needs a non-zero budget")
    {
        gdst_fixture gdst;
        gdst.width = 4;
        gdst.height = 4;
        gdst.data_format = test::support::image_format_rgba8; // no mipmaps bit: one level
        gdst.raw_data = godot_bytes(64, 0x5A);
        const godot_bytes gdst_image = build_gdst(gdst);

        gst2_fixture gst2;
        gst2.image_data_format = test::support::gst2_data_image;
        gst2.image_width = 4;
        gst2.image_height = 4;
        gst2.image_format = test::support::image_format_rgba8;
        gst2.raw_data = godot_bytes(64, 0x5A);
        const godot_bytes gst2_image = build_gst2(gst2);

        texture_limits none;
        none.max_mipmaps = 0;
        CHECK_THROWS_AS(probe(gdst_image, whole(gdst_image), none), texture_error);
        CHECK_THROWS_AS(probe(gst2_image, whole(gst2_image), none), texture_error);
    }
}

TEST_CASE("godot texture: declared mip counts must fit the canonical pyramid", "[godot][texture]")
{
    struct pyramid_case final
    {
        std::uint16_t width;
        std::uint16_t height;
        std::uint32_t levels;
    };
    const pyramid_case cases[] = {
        {1, 1, 1}, {8, 1, 4}, {1, 8, 4}, {4, 4, 3}, {8, 8, 4},
    };

    for (const pyramid_case &current : cases) {
        INFO("dims " << current.width << "x" << current.height);

        gdst_fixture exact;
        exact.width = current.width;
        exact.height = current.height;
        exact.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        exact.mipmap_count = current.levels;
        for (std::uint32_t level = 0; level < current.levels; ++level) {
            exact.mips.push_back(png_bytes(std::size_t{9} + level));
        }
        const godot_bytes exact_image = build_gdst(exact);
        const texture_payload payload = probe(exact_image, whole(exact_image));
        CHECK(payload.encoding == texture_encoding::png);
        CHECK(payload.body.offset == gdst_body_offset);

        gdst_fixture over;
        over.width = current.width;
        over.height = current.height;
        over.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        over.mipmap_count = current.levels + 1;
        for (std::uint32_t level = 0; level <= current.levels; ++level) {
            over.mips.push_back(png_bytes(std::size_t{9} + level));
        }
        const godot_bytes over_image = build_gdst(over);
        CHECK_THROWS_AS(probe(over_image, whole(over_image)), texture_error);
    }
}

TEST_CASE("godot texture: malformed envelopes are rejected", "[godot][texture]")
{
    SECTION("GDST bad magic")
    {
        gdst_fixture fixture;
        fixture.magic = "XXXX";
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 bad magic")
    {
        gst2_fixture fixture;
        fixture.magic = "GST1";
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 version newer than the supported maximum")
    {
        gst2_fixture fixture;
        fixture.version = 2;
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST truncated header")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        fixture.truncate_to = 10; // fewer than the 20 header bytes
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST base length runs past the source")
    {
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(8)};
        fixture.truncate_to = static_cast<std::size_t>(gdst_body_offset + 3); // three of eight bytes
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST raw block shorter than the declared format requires")
    {
        gdst_fixture fixture;
        fixture.width = 4;
        fixture.height = 4;
        fixture.data_format = test::support::image_format_rgba8; // needs 64 bytes
        fixture.raw_data = godot_bytes(10, 0x11);
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST zero mip count")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mipmap_count = 0;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST mip count over the limit")
    {
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mipmap_count = 64; // exceeds the 32-level limit
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 mip count over the limit")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_mipmaps = 64; // exceeds the 32-level limit
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 encoded mip runs past the source")
    {
        const godot_bytes base = png_bytes(11);
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_mipmaps = 1; // two declared levels
        fixture.mips = {base, png_bytes(6)};
        // Keep the second level's length field but only one of its declared bytes.
        fixture.truncate_to = static_cast<std::size_t>(gst2_prefix_bytes + 4 + base.size() + 4 + 1);
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 zero stored width")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 0;
        fixture.image_height = 8;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 zero stored height")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 0;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 stored dimension over the pixel budget")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 16384;
        fixture.image_height = 16384;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gst2(fixture);
        texture_limits limits;
        limits.max_pixels = 16ULL * 1024ULL * 1024ULL;
        CHECK_THROWS_AS(probe(image, whole(image), limits), texture_error);
    }

    SECTION("PNG payload shorter than its signature")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {godot_bytes{0x89, 'P', 'N', 'G'}};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("PNG signature mismatch")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {godot_bytes(16, 0x00)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST WebP payload shorter than its wrapper")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {godot_bytes{'W', 'E', 'B'}};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST WebP wrapper missing")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {godot_bytes(24, 0x11)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST WebP payload shorter than a RIFF container")
    {
        godot_bytes too_short = wrap_webp(godot_bytes(8, 0x00));
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {too_short};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("WebP payload is not a RIFF/WebP container")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {wrap_webp(godot_bytes(20, 0x00))};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("WebP payload has a RIFF fourcc but no WebP fourcc")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {wrap_webp(webp_riff_payload())};
        godot_bytes image = build_gdst(fixture);
        // Keep the leading "RIFF" fourcc valid but corrupt the trailing "WEBP" fourcc so the
        // second half of the container check is exercised.
        patch_u32(image, static_cast<std::size_t>(gdst_body_offset + 4 + 8), 0x58585858);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST encoded tail level with an invalid signature")
    {
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9), godot_bytes(9, 0x00)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST encoded WebP tail level missing its wrapper")
    {
        const godot_bytes riff = webp_riff_payload();
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {wrap_webp(riff), riff}; // the tail level omits the "WEBP" prefix
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 encoded tail level with a zero length")
    {
        const godot_bytes base = png_bytes(11);
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = {base, godot_bytes{}}; // second level declares zero bytes
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST encoded mip table overruns into an out-of-window read")
    {
        const godot_bytes base = png_bytes(11);
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mipmap_count = 3; // three declared levels so a following length field is read
        fixture.mips = {base, png_bytes(8)};
        godot_bytes image = build_gdst(fixture);
        // Inflate the second level's length so the third length field is read past the window.
        patch_u32(image, static_cast<std::size_t>(gdst_body_offset + base.size()), 0x7FFFFFFF);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST encoded final level length overruns the window")
    {
        const godot_bytes base = png_bytes(11);
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mipmap_count = 2;
        fixture.mips = {base, png_bytes(24)};
        godot_bytes image = build_gdst(fixture);
        // Inflate the last level's length so the table end lands past the window.
        patch_u32(image, static_cast<std::size_t>(gdst_body_offset + base.size()), 0x7FFFFFFF);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GDST declared level count exceeds a 1x1 image pyramid")
    {
        gdst_fixture fixture;
        fixture.width = 1;
        fixture.height = 1;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9), png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 raw declared mip count exceeds a 1x1 image pyramid")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_width = 1;
        fixture.image_height = 1;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.image_mipmaps = 31; // 32 levels declared for a 1x1 image
        fixture.raw_data = godot_bytes(4, 0x5A);
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 encoded declared level count exceeds a 1x1 image pyramid")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 1;
        fixture.image_height = 1;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = {png_bytes(9), png_bytes(9)}; // two declared levels for a 1x1 image
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("GST2 encoded final level length overruns the window")
    {
        const godot_bytes base = png_bytes(11);
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = {base, png_bytes(24)};
        godot_bytes image = build_gst2(fixture);
        // Inflate the last level's length so the table end lands past the window.
        patch_u32(image, static_cast<std::size_t>(gst2_prefix_bytes + 4 + base.size()), 0x7FFFFFFF);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("WebP RIFF length does not match the stored payload")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {wrap_webp(webp_riff_payload())};
        godot_bytes image = build_gdst(fixture);
        // The RIFF size field sits four bytes past the payload start (after "RIFF").
        patch_u32(image, static_cast<std::size_t>(gdst_body_offset + 4 + 4), 999);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }

    SECTION("source larger than the configured byte ceiling")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);

        texture_limits limits;
        limits.max_source_bytes = 8;
        check_envelope_error([&] { (void)probe(image, whole(image), limits); });
    }

    SECTION("source range extends past the stream")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, byte_range{0, image.size() + 1000}), texture_error);
    }

    SECTION("source range arithmetic overflows")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        const byte_range overflowing{std::numeric_limits<std::uint64_t>::max(), 2};
        check_envelope_error([&] { (void)probe(image, overflowing); });
    }

    SECTION("unreadable stream is translated to texture_error")
    {
        unreadable_stream stream(64);
        CHECK_THROWS_AS(probe_texture(stream, byte_range{0, 64}), texture_error);
    }
}

TEST_CASE("godot texture: unsupported families are rejected", "[godot][texture]")
{
    SECTION("GST2 Basis Universal")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_basis;
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }

    SECTION("GST2 unknown image data format")
    {
        gst2_fixture fixture;
        fixture.image_data_format = 7;
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }

    SECTION("GST2 raw HDR format (BC6H)")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_format = test::support::image_format_bptc_rgbf;
        fixture.raw_data = godot_bytes(16, 0x11);
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }

    SECTION("GST2 raw ASTC format")
    {
        gst2_fixture fixture;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_format = test::support::image_format_astc_4x4;
        fixture.raw_data = godot_bytes(16, 0x11);
        const godot_bytes image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }

    SECTION("GDST raw HDR format (BC6H)")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::image_format_bptc_rgbf;
        fixture.raw_data = godot_bytes(16, 0x11);
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }

    SECTION("GDST raw format outside the supported family")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::image_format_rgba4444;
        fixture.raw_data = godot_bytes(16, 0x11);
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }

    SECTION("GDST unknown raw ordinal")
    {
        gdst_fixture fixture;
        fixture.data_format = 100;
        fixture.raw_data = godot_bytes(16, 0x11);
        const godot_bytes image = build_gdst(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }
}

TEST_CASE("godot texture: probing does not read the whole encoded body", "[godot][texture]")
{
    const godot_bytes body = png_bytes(4096);
    gdst_fixture fixture;
    fixture.width = 64;
    fixture.height = 64;
    fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
    fixture.mips = {body};
    const godot_bytes image = build_gdst(fixture);

    counting_stream stream(as_text(image));
    const texture_payload payload = probe_texture(stream, whole(image));

    CHECK(payload.body.size == body.size());
    CHECK(stream.bytes_read() < image.size());
    // Header plus at most a small fixed signature: the 4 KiB body is never consumed.
    CHECK(stream.bytes_read() <= 128U);
}

TEST_CASE("godot texture: a source-stream fault is a distinct texture_source_error", "[godot][texture]")
{
    // The prober must separate a source read/seek failure (a caller maps it to a source read
    // error) from a malformed envelope (a declined conversion). Both derive from texture_error,
    // so the subtype stays catch-compatible with the existing base-type handling.

    SECTION("source window extends beyond the physical stream size")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        // The declared window reaches past the bytes the stream actually holds.
        CHECK_THROWS_AS(probe(image, byte_range{0, image.size() + 1000}), texture_source_error);
    }

    SECTION("unreadable stream with default state flags")
    {
        unreadable_stream stream(64);
        CHECK_THROWS_AS(probe_texture(stream, byte_range{0, 64}), texture_source_error);
    }

    SECTION("unreadable stream with exceptions enabled")
    {
        unreadable_stream stream(64);
        stream.exceptions(static_cast<std::ios_base::iostate>(static_cast<unsigned>(std::ios::failbit) |
                                                              static_cast<unsigned>(std::ios::badbit)));
        CHECK_THROWS_AS(probe_texture(stream, byte_range{0, 64}), texture_source_error);
    }

    SECTION("seek fault while locating the physical stream")
    {
        faulting_stream stream;
        CHECK_THROWS_AS(probe_texture(stream, byte_range{0, 64}), texture_source_error);
    }
}

TEST_CASE("godot texture: malformed readable envelopes stay an ordinary texture_error", "[godot][texture]")
{
    SECTION("GDST truncated header")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        fixture.truncate_to = 10; // fewer bytes than the 20-byte header, all within the window
        const godot_bytes image = build_gdst(fixture);
        check_envelope_error([&] { (void)probe(image, whole(image)); });
    }

    SECTION("GDST bad magic")
    {
        gdst_fixture fixture;
        fixture.magic = "XXXX";
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {png_bytes(9)};
        const godot_bytes image = build_gdst(fixture);
        check_envelope_error([&] { (void)probe(image, whole(image)); });
    }

    SECTION("PNG signature mismatch")
    {
        gdst_fixture fixture;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = {godot_bytes(16, 0x00)};
        const godot_bytes image = build_gdst(fixture);
        check_envelope_error([&] { (void)probe(image, whole(image)); });
    }
}

TEST_CASE("godot texture: scaled GST2 selects encoded format from content", "[godot][texture-media]")
{
    for (const bool png : {true, false}) {
        gst2_fixture fixture;
        fixture.scale_tag = 2;
        fixture.image_width = 8;
        fixture.image_height = 4;
        fixture.image_data_format = png ? test::support::gst2_data_png : test::support::gst2_data_webp;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = {png ? png_bytes(16) : webp_riff_payload()};
        const auto image = build_gst2(fixture);
        for (const std::size_t lead : {std::size_t{0}, std::size_t{7}}) {
            const auto shifted = offset_by(lead, image);
            const auto payload = probe(shifted, byte_range{lead, image.size()});
            CHECK(payload.encoding == (png ? texture_encoding::png : texture_encoding::webp));
            CHECK(payload.body.offset == lead + 58);
            CHECK(payload.width == 8);
            CHECK(payload.height == 4);
            CHECK(bytes_at(shifted, payload.body) == fixture.mips[0]);
        }
    }
}

TEST_CASE("godot texture: scaled GST2 supports raw data and modern RA-as-RG", "[godot][texture-media]")
{
    for (const bool scaled : {false, true}) {
        gst2_fixture fixture;
        if (scaled)
            fixture.scale_tag = 2;
        fixture.image_width = 4;
        fixture.image_height = 4;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_format = 34;
        fixture.raw_data = godot_bytes(16, 0x11);
        const auto image = build_gst2(fixture);
        const auto payload = probe(image, whole(image));
        CHECK(payload.encoding == texture_encoding::raw);
        CHECK(payload.wire_image_format == 34);
        CHECK(payload.body.offset == (scaled ? 54 : 52));
        CHECK(bytes_at(image, payload.body) == fixture.raw_data.value());
    }
    gdst_fixture legacy;
    legacy.width = legacy.height = 4;
    legacy.data_format = 34;
    legacy.raw_data = godot_bytes(16, 0);
    const auto image = build_gdst(legacy);
    CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
}

TEST_CASE("godot texture: default probe accepts an encoded image above sixteen MiPixels", "[godot][texture-media]")
{
    gst2_fixture fixture;
    fixture.image_width = 5000;
    fixture.image_height = 4000;
    fixture.image_data_format = test::support::gst2_data_png;
    fixture.mips = {png_bytes(16)};
    const auto image = build_gst2(fixture);
    counting_stream stream(as_text(image));
    const auto payload = probe_texture(stream, whole(image));
    CHECK(payload.width == 5000);
    CHECK(payload.height == 4000);
    CHECK(payload.body.size == 16);
    CHECK(stream.bytes_read() < 128);
}

TEST_CASE("godot texture: default probe accepts a large physical window without reading its tail",
          "[godot][texture-media]")
{
    // Models a sparse source with a complete small encoded level and a large unused physical tail.
    class extent_buffer final : public std::stringbuf
    {
      public:
        extent_buffer(std::string prefix, const std::streamoff extent)
            : std::stringbuf(std::move(prefix), std::ios::in), extent_(extent)
        {
        }

      protected:
        pos_type seekoff(const off_type offset, const std::ios_base::seekdir direction,
                         const std::ios_base::openmode mode) override
        {
            if (direction == std::ios::end && offset == 0) {
                virtual_end_ = true;
                return pos_type(extent_);
            }
            if (direction == std::ios::cur && offset == 0 && virtual_end_)
                return pos_type(extent_);
            virtual_end_ = false;
            return std::stringbuf::seekoff(offset, direction, mode);
        }
        pos_type seekpos(const pos_type position, const std::ios_base::openmode mode) override
        {
            virtual_end_ = false;
            return std::stringbuf::seekpos(position, mode);
        }

      private:
        std::streamoff extent_;
        bool virtual_end_ = false;
    };
    gst2_fixture fixture;
    fixture.mips = {png_bytes(16)};
    const auto image = build_gst2(fixture);
    constexpr std::uint64_t extent = 300ULL * 1024 * 1024;
    extent_buffer buffer(as_text(image), static_cast<std::streamoff>(extent));
    std::istream stream(&buffer);
    const auto payload = probe_texture(stream, byte_range{0, extent});
    CHECK(payload.body.offset == 56);
    CHECK(payload.body.size == 16);
}

TEST_CASE("godot texture: scaled metadata retains bounds and unsupported-format fallback", "[godot][texture-media]")
{
    SECTION("recognized Basis remains unsupported")
    {
        gst2_fixture fixture;
        fixture.scale_tag = 2;
        fixture.image_data_format = test::support::gst2_data_basis;
        fixture.raw_data = godot_bytes(16, 0);
        const auto image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }
    SECTION("unknown content kind is never guessed as WebP")
    {
        gst2_fixture fixture;
        fixture.scale_tag = 2;
        fixture.image_data_format = 7;
        fixture.raw_data = godot_bytes(16, 0);
        const auto image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_unsupported);
    }
    SECTION("raw content still requires the full pixel block")
    {
        gst2_fixture fixture;
        fixture.scale_tag = 2;
        fixture.image_width = fixture.image_height = 4;
        fixture.image_data_format = test::support::gst2_data_image;
        fixture.image_format = 34;
        fixture.raw_data = godot_bytes(15, 0);
        const auto image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }
    SECTION("a missing encoded length remains a malformed envelope")
    {
        gst2_fixture fixture;
        fixture.scale_tag = 2;
        fixture.mips = {png_bytes(16)};
        fixture.truncate_to = 54;
        const auto image = build_gst2(fixture);
        CHECK_THROWS_AS(probe(image, whole(image)), texture_error);
    }
    for (const bool width : {true, false}) {
        SECTION(width ? "zero width never yields a conversion" : "zero height never yields a conversion")
        {
            gst2_fixture fixture;
            fixture.scale_tag = 2;
            fixture.image_width = width ? 0 : 4;
            fixture.image_height = width ? 4 : 0;
            fixture.mips = {png_bytes(16)};
            const auto image = build_gst2(fixture);
            CHECK_THROWS(probe(image, whole(image)));
        }
    }
}
