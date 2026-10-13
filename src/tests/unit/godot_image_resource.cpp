#include "../../core/codecs/bc_codec.h"
#include "../../core/codecs/png_codec.h"
#include "../../core/godot/binary_resource.h"
#include "../../modules/godot/image_resource.h"
#include "../support/godot_resource_fixtures.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <initializer_list>
#include <limits>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#ifdef _DEBUG
#include <crtdbg.h>
#endif

// The tests read the produced PNG streams with libspng directly so the converter is judged
// against an independent decoder. libspng is a static port, so its declarations must not carry
// the dllimport decoration; the macro stays private to this translation unit.
#define SPNG_STATIC
#include <spng.h>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using test::support::build_resource;
    using test::support::godot_resource_bytes;
    using test::support::resource_document_spec;
    using test::support::resource_property_spec;
    using test::support::resource_record_spec;
    using test::support::variant_encoder;
    using test::support::wire_writer;

    using unpackrat::codecs::bc_format;
    using unpackrat::codecs::image_error;
    using unpackrat::codecs::image_limits;
    using unpackrat::codecs::rgba_image;
    using unpackrat::godot::binary_resource;
    using unpackrat::godot::image_resource_extension;
    using unpackrat::godot::read_binary_resource;
    using unpackrat::godot::resource_property;
    using unpackrat::godot::resource_record;
    using unpackrat::godot::supports_raw_image;
    using unpackrat::godot::value_kind;
    using unpackrat::godot::write_image_resource;
    using unpackrat::godot::write_raw_image;

    std::span<const std::byte> as_bytes(const godot_resource_bytes &bytes)
    {
        return {reinterpret_cast<const std::byte *>(bytes.data()), bytes.size()};
    }

    binary_resource read_resource(const godot_resource_bytes &bytes)
    {
        return read_binary_resource(as_bytes(bytes));
    }

    std::vector<std::byte> byte_list(const std::initializer_list<unsigned int> values)
    {
        std::vector<std::byte> result;
        result.reserve(values.size());
        for (const unsigned int value : values) {
            result.push_back(static_cast<std::byte>(value));
        }
        return result;
    }

    std::vector<std::uint8_t> u8_list(const std::initializer_list<unsigned int> values)
    {
        std::vector<std::uint8_t> result;
        result.reserve(values.size());
        for (const unsigned int value : values) {
            result.push_back(static_cast<std::uint8_t>(value));
        }
        return result;
    }

    std::vector<std::byte> as_byte_vector(const std::span<const std::uint8_t> values)
    {
        std::vector<std::byte> result;
        result.reserve(values.size());
        for (const std::uint8_t value : values) {
            result.push_back(static_cast<std::byte>(value));
        }
        return result;
    }

    struct decoded_png final
    {
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::vector<std::byte> pixels;
    };

    // Decodes a PNG stream into RGBA8 with libspng; the usage is test-only and does not exercise
    // production code.
    decoded_png decode_png(const std::span<const std::byte> stream)
    {
        spng_ctx *context = spng_ctx_new(0);
        REQUIRE(context != nullptr);
        struct context_guard final
        {
            spng_ctx *context;
            ~context_guard()
            {
                spng_ctx_free(context);
            }
        } guard{context};

        REQUIRE(spng_set_png_buffer(context, stream.data(), stream.size()) == SPNG_OK);
        spng_ihdr header{};
        REQUIRE(spng_get_ihdr(context, &header) == SPNG_OK);
        std::size_t decoded_size = 0;
        REQUIRE(spng_decoded_image_size(context, SPNG_FMT_RGBA8, &decoded_size) == SPNG_OK);

        decoded_png result;
        result.width = header.width;
        result.height = header.height;
        result.pixels.resize(decoded_size);
        REQUIRE(spng_decode_image(context, result.pixels.data(), result.pixels.size(), SPNG_FMT_RGBA8, 0) == SPNG_OK);
        return result;
    }

    std::vector<std::byte> write_raw(const std::span<const std::byte> top_mip, const std::uint32_t engine_major,
                                     const std::uint32_t wire_format, const std::uint32_t width,
                                     const std::uint32_t height, const image_limits &limits = {})
    {
        std::vector<std::byte> stream;
        write_raw_image(
            top_mip, engine_major, wire_format, width, height,
            [&](const std::span<const std::byte> chunk) { stream.insert(stream.end(), chunk.begin(), chunk.end()); },
            limits);
        return stream;
    }

    std::vector<std::byte> write_resource(const binary_resource &resource, const image_limits &limits = {})
    {
        std::vector<std::byte> stream;
        write_image_resource(
            resource,
            [&](const std::span<const std::byte> chunk) { stream.insert(stream.end(), chunk.begin(), chunk.end()); },
            limits);
        if (const auto info = unpackrat::godot::inspect_image_resource(resource, limits); info.has_value()) {
            if (info->output_size.has_value()) {
                CHECK(info->output_size == stream.size());
                CHECK(info->decode_bytes == 0);
            } else {
                CHECK(info->extension == ".png");
                CHECK(info->decode_bytes > 0);
            }
        } else {
            FAIL("a successful image write must have an inspectable descriptor");
        }
        return stream;
    }

    template <typename Encode> godot_resource_bytes encode(const bool modern, Encode encode_step)
    {
        wire_writer out;
        variant_encoder encoder(out, modern, false);
        encode_step(encoder);
        return out.take();
    }

    resource_document_spec document(const std::uint32_t major, const std::uint32_t format_version)
    {
        resource_document_spec spec;
        spec.engine_major = major;
        spec.format_version = format_version;
        if (major == 2) {
            spec.engine_minor = 1;
            spec.string_table.push_back("image");
        } else {
            spec.engine_minor = major == 4 ? 5U : 6U;
        }
        return spec;
    }

    resource_property_spec property(std::string name, godot_resource_bytes value)
    {
        resource_property_spec spec;
        spec.name = std::move(name);
        spec.value = std::move(value);
        return spec;
    }

    resource_record_spec record(std::string path, std::string type, std::vector<resource_property_spec> properties)
    {
        resource_record_spec spec;
        spec.path = std::move(path);
        spec.type = std::move(type);
        spec.properties = std::move(properties);
        return spec;
    }

    // Builds a document whose header main type matches its root record, which is what the engine
    // writes; a test that needs the deliberate mismatch sets ``main_type`` itself and calls
    // ``build_resource`` directly.
    godot_resource_bytes build_canonical(resource_document_spec spec)
    {
        if (!spec.records.empty()) {
            spec.main_type = spec.records.back().type;
        }
        return build_resource(spec);
    }

    // A Godot 2 ImageTexture whose "image" property is a raw embedded Image variant.
    godot_resource_bytes g2_raw_texture(const std::uint32_t width, const std::uint32_t height,
                                        const std::uint32_t mipmaps, const std::uint32_t format,
                                        const std::span<const std::uint8_t> data)
    {
        resource_document_spec spec = document(2, 1);
        auto &record_ref = spec.records.emplace_back();
        record_ref.path = "res://tex";
        record_ref.type = "ImageTexture";
        record_ref.properties.push_back(property("image", encode(false, [&](variant_encoder &encoder) {
                                                     encoder.image_raw(width, height, mipmaps, format, data);
                                                 })));
        return build_canonical(spec);
    }

    // A Godot 2 ImageTexture whose "image" property is a preserved encoded Image variant.
    godot_resource_bytes g2_encoded_texture(const std::uint32_t encoding, const std::span<const std::uint8_t> data)
    {
        resource_document_spec spec = document(2, 1);
        auto &record_ref = spec.records.emplace_back();
        record_ref.path = "res://tex";
        record_ref.type = "ImageTexture";
        record_ref.properties.push_back(
            property("image", encode(false, [&](variant_encoder &encoder) { encoder.image_encoded(encoding, data); })));
        return build_canonical(spec);
    }

    godot_resource_bytes vstring(const bool modern, const std::string_view value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.string(value); });
    }

    godot_resource_bytes vint(const bool modern, const std::int32_t value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.int32(value); });
    }

    godot_resource_bytes vint64(const bool modern, const std::int64_t value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.int64(value); });
    }

    godot_resource_bytes vbool(const bool modern, const bool value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.boolean(value); });
    }

    godot_resource_bytes vbytes(const bool modern, const std::span<const std::uint8_t> value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.bytes(value); });
    }

    godot_resource_bytes dictionary(const bool modern, const std::vector<godot_resource_bytes> &keys_and_values)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.dictionary(keys_and_values); });
    }

    // The Image "data" dictionary as the engine writes it, with each value pre-encoded so a test
    // can inject a wrong-typed or missing field.
    godot_resource_bytes image_data(const bool modern, const std::vector<godot_resource_bytes> &keys_and_values)
    {
        return dictionary(modern, keys_and_values);
    }

    godot_resource_bytes standard_image_data(const bool modern, const std::int32_t width, const std::int32_t height,
                                             const std::string_view format, const bool mipmaps,
                                             const std::span<const std::uint8_t> data)
    {
        return image_data(modern, {vstring(modern, "width"), vint(modern, width), vstring(modern, "height"),
                                   vint(modern, height), vstring(modern, "format"), vstring(modern, format),
                                   vstring(modern, "mipmaps"), vbool(modern, mipmaps), vstring(modern, "data"),
                                   vbytes(modern, data)});
    }

    // A Godot 3/4 ImageTexture that references an internal Image record through the legacy
    // "local://0" subresource id.
    godot_resource_bytes modern_texture(const std::uint32_t major, const std::uint32_t format_version,
                                        const godot_resource_bytes &data_dictionary)
    {
        const bool modern = major >= 4;
        resource_document_spec spec = document(major, format_version);
        spec.records.push_back(record("local://0", "Image", {property("data", data_dictionary)}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        return build_canonical(spec);
    }

    // A Godot 3/4 ImageTexture referencing an arbitrary internal directory index.
    godot_resource_bytes modern_texture_reference(const std::uint32_t major, const std::uint32_t format_version,
                                                  const std::uint32_t index)
    {
        resource_document_spec spec = document(major, format_version);
        // Positional addressing (FORMAT_FLAG_NAMED_SCENE_IDS) so the wire index is the directory
        // index directly, letting the test aim at any record.
        spec.named_scene_ids = true;
        spec.records.push_back(record("res://child", "Image", {}));
        spec.records.push_back(record(
            "res://tex", "ImageTexture",
            {property("image", encode(true, [&](variant_encoder &encoder) { encoder.object_internal(index); }))}));
        return build_canonical(spec);
    }
} // namespace

TEST_CASE("godot image resource: maps supported raw wire formats per engine")
{
    using std::uint32_t;
    for (const uint32_t format : {0U, 1U, 2U, 3U, 4U}) {
        CHECK(supports_raw_image(2, format));
    }
    for (const uint32_t format : {5U, 6U, 12U, 30U, 0xFFFFFFFFU}) {
        CHECK(!(supports_raw_image(2, format)));
    }
    for (const uint32_t format : {7U, 8U, 9U, 10U, 11U}) {
        CHECK(supports_raw_image(2, format));
    }
    for (const uint32_t format : {0U, 1U, 2U, 3U, 4U, 5U, 6U, 7U, 17U, 18U, 19U, 20U, 21U, 22U}) {
        CHECK(supports_raw_image(3, format));
        CHECK(supports_raw_image(4, format));
    }
    for (const uint32_t format : {8U, 16U, 23U, 25U}) {
        CHECK(!(supports_raw_image(3, format)));
        CHECK(!(supports_raw_image(4, format)));
    }
    CHECK(!(supports_raw_image(1, 0)));
    CHECK(!(supports_raw_image(5, 5)));
}

TEST_CASE("godot image resource: expands Godot 2 raw wire formats to RGBA8")
{
    SECTION("grayscale")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 200}), 2, 0, 2, 1));
        REQUIRE(decoded.pixels == byte_list({10, 10, 10, 255, 200, 200, 200, 255}));
    }
    SECTION("intensity is white with the value as alpha")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 200}), 2, 1, 2, 1));
        REQUIRE(decoded.pixels == byte_list({255, 255, 255, 10, 255, 255, 255, 200}));
    }
    SECTION("grayscale with alpha")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 20, 200, 255}), 2, 2, 2, 1));
        REQUIRE(decoded.pixels == byte_list({10, 10, 10, 20, 200, 200, 200, 255}));
    }
    SECTION("rgb")
    {
        const auto decoded = decode_png(write_raw(byte_list({1, 2, 3, 4, 5, 6}), 2, 3, 2, 1));
        REQUIRE(decoded.pixels == byte_list({1, 2, 3, 255, 4, 5, 6, 255}));
    }
    SECTION("rgba")
    {
        const auto decoded = decode_png(write_raw(byte_list({1, 2, 3, 4, 5, 6, 7, 8}), 2, 4, 2, 1));
        REQUIRE(decoded.pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
    }
}

TEST_CASE("godot image resource: expands Godot 3/4 raw wire formats to RGBA8")
{
    SECTION("luminance")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 200}), 3, 0, 2, 1));
        REQUIRE(decoded.pixels == byte_list({10, 10, 10, 255, 200, 200, 200, 255}));
    }
    SECTION("luminance with alpha")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 20, 200, 255}), 3, 1, 2, 1));
        REQUIRE(decoded.pixels == byte_list({10, 10, 10, 20, 200, 200, 200, 255}));
    }
    SECTION("red only")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 200}), 3, 2, 2, 1));
        REQUIRE(decoded.pixels == byte_list({10, 0, 0, 255, 200, 0, 0, 255}));
    }
    SECTION("red-green")
    {
        const auto decoded = decode_png(write_raw(byte_list({10, 20, 200, 255}), 3, 3, 2, 1));
        REQUIRE(decoded.pixels == byte_list({10, 20, 0, 255, 200, 255, 0, 255}));
    }
    SECTION("rgb8 and rgba8")
    {
        const auto rgb = decode_png(write_raw(byte_list({1, 2, 3, 4, 5, 6}), 4, 4, 2, 1));
        REQUIRE(rgb.pixels == byte_list({1, 2, 3, 255, 4, 5, 6, 255}));
        const auto rgba = decode_png(write_raw(byte_list({1, 2, 3, 4, 5, 6, 7, 8}), 4, 5, 2, 1));
        REQUIRE(rgba.pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
    }
    SECTION("rgba4444 unpacks each nibble by seventeen")
    {
        // 0xABCD: r=0xA g=0xB b=0xC a=0xD, stored little endian.
        for (const std::uint32_t major : {3U, 4U}) {
            const auto decoded = decode_png(write_raw(byte_list({0xCD, 0xAB}), major, 6, 1, 1));
            REQUIRE(decoded.pixels == byte_list({170, 187, 204, 221}));
        }
    }
    SECTION("godot 3 rgba5551 saturates each 5-bit field at seventeen")
    {
        // 0xF821: r=31 g=0 b=16 a=1 -> r,b saturate, g stays zero.
        const auto saturated = decode_png(write_raw(byte_list({0x21, 0xF8}), 3, 7, 1, 1));
        REQUIRE(saturated.pixels == byte_list({255, 0, 255, 255}));
        // 0x5294: r=g=b=10 (in range), alpha bit clear.
        const auto in_range = decode_png(write_raw(byte_list({0x94, 0x52}), 3, 7, 1, 1));
        REQUIRE(in_range.pixels == byte_list({170, 170, 170, 0}));
    }
    SECTION("godot 4 rgb565 scales 5/6/5 fields by 255 over their range")
    {
        // 0x8410: r=16 (of 31), g=32 (of 63), b=16 (of 31).
        const auto decoded = decode_png(write_raw(byte_list({0x10, 0x84}), 4, 7, 1, 1));
        REQUIRE(decoded.pixels == byte_list({131, 129, 131, 255}));
        const auto opaque = decode_png(write_raw(byte_list({0x00, 0x00}), 4, 7, 1, 1));
        REQUIRE(opaque.pixels == byte_list({0, 0, 0, 255}));
    }
}

TEST_CASE("godot image resource: decodes block-compressed base mips to the decoded pixels")
{
    const auto bc1 = byte_list({0xFF, 0xFF, 0x00, 0x00, 0xE4, 0x00, 0x00, 0x00});
    const auto bc2 =
        byte_list({0x0F, 0xF0, 0xAA, 0x55, 0x00, 0xFF, 0x33, 0xCC, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto bc3 =
        byte_list({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto bc4 = byte_list({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00});
    const auto bc5 =
        byte_list({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00, 0xFF, 0x00, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00});
    const auto bc7 =
        byte_list({0x40, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF});

    SECTION("bc1")
    {
        const auto expected = unpackrat::codecs::decode_bc(bc1, bc_format::bc1, 4, 4);
        const auto decoded = decode_png(write_raw(bc1, 3, 17, 4, 4));
        REQUIRE(decoded.pixels == expected.pixels);
    }
    SECTION("bc2")
    {
        const auto expected = unpackrat::codecs::decode_bc(bc2, bc_format::bc2, 4, 4);
        const auto decoded = decode_png(write_raw(bc2, 3, 18, 4, 4));
        REQUIRE(decoded.pixels == expected.pixels);
    }
    SECTION("bc3")
    {
        const auto expected = unpackrat::codecs::decode_bc(bc3, bc_format::bc3, 4, 4);
        const auto decoded = decode_png(write_raw(bc3, 3, 19, 4, 4));
        REQUIRE(decoded.pixels == expected.pixels);
    }
    SECTION("bc4 expands the single channel")
    {
        const auto decoded = decode_png(write_raw(bc4, 3, 20, 4, 4));
        REQUIRE(decoded.pixels.size() == 64);
        for (std::size_t index = 0; index < 16; ++index) {
            REQUIRE(decoded.pixels[index * 4 + 1] == std::byte{0});
            REQUIRE(decoded.pixels[index * 4 + 2] == std::byte{0});
            REQUIRE(decoded.pixels[index * 4 + 3] == std::byte{255});
        }
    }
    SECTION("bc5 expands two channels")
    {
        const auto expected = unpackrat::codecs::decode_bc(bc5, bc_format::bc5, 4, 4);
        const auto decoded = decode_png(write_raw(bc5, 4, 21, 4, 4));
        REQUIRE(decoded.pixels == expected.pixels);
    }
    SECTION("bc7")
    {
        const auto expected = unpackrat::codecs::decode_bc(bc7, bc_format::bc7, 4, 4);
        const auto decoded = decode_png(write_raw(bc7, 4, 22, 4, 4));
        REQUIRE(decoded.pixels == expected.pixels);
    }
    SECTION("godot 2 bc1 uses the legacy wire id")
    {
        const auto expected = unpackrat::codecs::decode_bc(bc1, bc_format::bc1, 4, 4);
        const auto decoded = decode_png(write_raw(bc1, 2, 7, 4, 4));
        REQUIRE(decoded.pixels == expected.pixels);
    }
    SECTION("a partial edge is padded to whole blocks")
    {
        std::vector<std::byte> grid;
        for (const auto *block : {&bc1, &bc1, &bc1, &bc1}) {
            grid.insert(grid.end(), block->begin(), block->end());
        }
        const auto expected = unpackrat::codecs::decode_bc(grid, bc_format::bc1, 6, 5);
        const auto decoded = decode_png(write_raw(grid, 3, 17, 6, 5));
        REQUIRE(decoded.width == 6);
        REQUIRE(decoded.height == 5);
        REQUIRE(decoded.pixels == expected.pixels);
    }
}

TEST_CASE("godot image resource: raw writes start with the PNG signature")
{
    const auto stream = write_raw(byte_list({1, 2, 3, 4}), 4, 5, 1, 1);
    REQUIRE(stream.size() > 8);
    REQUIRE(stream[0] == std::byte{0x89});
    REQUIRE(stream[1] == std::byte{0x50});
    REQUIRE(stream[2] == std::byte{0x4E});
    REQUIRE(stream[3] == std::byte{0x47});
}

TEST_CASE("godot image resource: raw writes reject malformed descriptions")
{
    const auto top = byte_list({1, 2, 3, 4});
    SECTION("unsupported format")
    {
        REQUIRE_THROWS_AS(write_raw(top, 3, 8, 1, 1), image_error);
    }
    SECTION("zero width")
    {
        REQUIRE_THROWS_AS(write_raw(top, 4, 5, 0, 1), image_error);
    }
    SECTION("zero height")
    {
        REQUIRE_THROWS_AS(write_raw(top, 4, 5, 1, 0), image_error);
    }
    SECTION("short base mip")
    {
        REQUIRE_THROWS_AS(write_raw(byte_list({1, 2, 3}), 4, 5, 1, 1), image_error);
    }
    SECTION("long base mip")
    {
        REQUIRE_THROWS_AS(write_raw(byte_list({1, 2, 3, 4, 5}), 4, 5, 1, 1), image_error);
    }
    SECTION("pixel budget exceeded")
    {
        image_limits limits;
        limits.max_pixels = 3;
        REQUIRE_THROWS_AS(write_raw(byte_list({1, 2, 3, 4}), 4, 5, 2, 2, limits), image_error);
    }
    SECTION("padded block grid overflows a 64-bit byte count")
    {
        image_limits limits;
        limits.max_pixels = (std::numeric_limits<std::size_t>::max)();
        REQUIRE_THROWS_AS(write_raw({}, 3, 17, 0xFFFFFFFFU, 0xFFFFFFFFU, limits), image_error);
    }
    SECTION("padded byte count overflows a 64-bit result")
    {
        image_limits limits;
        limits.max_pixels = (std::numeric_limits<std::size_t>::max)();
        REQUIRE_THROWS_AS(write_raw({}, 4, 5, 0x80000000U, 0x80000000U, limits), image_error);
    }
    SECTION("the sink's own exception propagates unchanged")
    {
        class sink_failure final : public std::runtime_error
        {
          public:
            using std::runtime_error::runtime_error;
        };
        bool caught = false;
        try {
            write_raw_image(top, 4, 5, 1, 1,
                            [](std::span<const std::byte>) { throw sink_failure("sink rejected the chunk"); });
        } catch (const sink_failure &error) {
            caught = true;
            REQUIRE(std::string(error.what()) == "sink rejected the chunk");
        }
        REQUIRE(caught);
    }
}

TEST_CASE("godot image resource: preserves a Godot 2 embedded PNG stream")
{
    // Encode a known image with the accepted PNG codec, wrap it in the Godot 2 "PNG " envelope,
    // and require the converter to recover the inner file byte for byte.
    rgba_image source;
    source.width = 2;
    source.height = 1;
    source.pixels = byte_list({1, 2, 3, 4, 5, 6, 7, 8});
    std::vector<std::uint8_t> inner;
    unpackrat::codecs::encode_png(source, [&](const std::span<const std::byte> chunk) {
        for (const std::byte value : chunk) {
            inner.push_back(static_cast<std::uint8_t>(value));
        }
    });
    std::vector<std::uint8_t> envelope = u8_list({'P', 'N', 'G', ' '});
    envelope.insert(envelope.end(), inner.begin(), inner.end());

    const binary_resource resource = read_resource(g2_encoded_texture(2, envelope));
    const auto extension = image_resource_extension(resource);
    REQUIRE(extension.has_value());
    REQUIRE(*extension == ".png");

    const auto stream = write_resource(resource);
    REQUIRE(stream == as_byte_vector(inner));
    const auto decoded = decode_png(stream);
    REQUIRE(decoded.pixels == source.pixels);
}

TEST_CASE("godot image resource: preserves a Godot 2 embedded WebP stream")
{
    // A minimal, structurally valid RIFF/WEBP container; only the bounded header is validated.
    const std::vector<std::uint8_t> webp =
        u8_list({'R', 'I', 'F', 'F', 8, 0, 0, 0, 'W', 'E', 'B', 'P', 'T', 'E', 'S', 'T'});
    std::vector<std::uint8_t> envelope = u8_list({'W', 'E', 'B', 'P'});
    envelope.insert(envelope.end(), webp.begin(), webp.end());

    const binary_resource resource = read_resource(g2_encoded_texture(3, envelope));
    const auto extension = image_resource_extension(resource);
    REQUIRE(extension.has_value());
    REQUIRE(*extension == ".webp");
    REQUIRE(write_resource(resource) == as_byte_vector(webp));
}

TEST_CASE("godot image resource: rejects malformed Godot 2 encoded envelopes")
{
    SECTION("png envelope without the leading marker")
    {
        const std::vector<std::uint8_t> payload = u8_list({0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(2, payload))), image_error);
    }
    SECTION("png envelope whose inner file lacks the signature")
    {
        std::vector<std::uint8_t> payload = u8_list({'P', 'N', 'G', ' '});
        payload.insert(payload.end(), 8, 0);
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(2, payload))), image_error);
    }
    SECTION("webp envelope without the leading marker")
    {
        const std::vector<std::uint8_t> payload = u8_list({'R', 'I', 'F', 'F', 4, 0, 0, 0, 'W', 'E', 'B', 'P'});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, payload))), image_error);
    }
    SECTION("webp envelope whose RIFF size exceeds the payload")
    {
        std::vector<std::uint8_t> payload =
            u8_list({'W', 'E', 'B', 'P', 'R', 'I', 'F', 'F', 100, 0, 0, 0, 'W', 'E', 'B', 'P'});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, payload))), image_error);
    }
    SECTION("an unknown encoding is rejected")
    {
        const std::vector<std::uint8_t> payload = u8_list({1, 2, 3, 4});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(7, payload))), image_error);
    }
}

TEST_CASE("godot image resource: converts a Godot 2 raw embedded image")
{
    const std::uint32_t format = 4; // RGBA
    const std::vector<std::uint8_t> data = u8_list({1, 2, 3, 4, 5, 6, 7, 8});
    const binary_resource resource = read_resource(g2_raw_texture(2, 1, 0, format, data));
    const auto extension = image_resource_extension(resource);
    REQUIRE(extension.has_value());
    REQUIRE(*extension == ".png");
    const auto decoded = decode_png(write_resource(resource));
    REQUIRE(decoded.width == 2);
    REQUIRE(decoded.height == 1);
    REQUIRE(decoded.pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
}

TEST_CASE("godot image resource: a Godot 2 raw length must match the declared mip geometry")
{
    const std::vector<std::uint8_t> short_data = u8_list({1, 2, 3, 4, 5});
    REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 1, 0, 4, short_data))), image_error);

    // mipmaps is an additional-level count: one extra level is a single 1x1 RGBA texel (4 bytes),
    // so a 2x1 RGBA base of 8 bytes plus one level is exactly 12 bytes.
    std::vector<std::uint8_t> one_level = u8_list({1, 2, 3, 4, 5, 6, 7, 8});
    one_level.insert(one_level.end(), 4, 0);
    const binary_resource resource = read_resource(g2_raw_texture(2, 1, 1, 4, one_level));
    REQUIRE(image_resource_extension(resource).value_or("") == ".png");
    REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));

    // Five arbitrary trailing bytes are not a valid one-level chain.
    std::vector<std::uint8_t> arbitrary = u8_list({1, 2, 3, 4, 5, 6, 7, 8});
    arbitrary.insert(arbitrary.end(), 5, 0);
    REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 1, 1, 4, arbitrary))), image_error);
}

TEST_CASE("godot image resource: reports class and shape mismatches without an image")
{
    SECTION("a record set without a root is malformed")
    {
        resource_document_spec spec = document(3, 3);
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
    }
    SECTION("an unsupported engine generation yields no image")
    {
        binary_resource resource;
        resource.engine.major = 5;
        REQUIRE(!(image_resource_extension(resource).has_value()));
    }
    SECTION("a Godot 2 root of another class yields no image")
    {
        resource_document_spec spec = document(2, 1);
        spec.records.push_back(record("res://root", "Resource", {}));
        REQUIRE(!(image_resource_extension(read_resource(build_canonical(spec))).has_value()));
    }
    SECTION("a Godot 3 ImageTexture without an image property is malformed")
    {
        resource_document_spec spec = document(3, 3);
        spec.records.push_back(record("res://tex", "ImageTexture", {}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
    }
    SECTION("a Godot 3 ImageTexture whose image is not an object is malformed")
    {
        resource_document_spec spec = document(3, 3);
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(false, [](variant_encoder &encoder) { encoder.int32(7); }))}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
    }
    SECTION("a Godot 2 empty image yields no image")
    {
        resource_document_spec spec = document(2, 1);
        auto &record_ref = spec.records.emplace_back();
        record_ref.path = "res://tex";
        record_ref.type = "ImageTexture";
        record_ref.properties.push_back(
            property("image", encode(false, [](variant_encoder &encoder) { encoder.image_encoded(0, {}); })));
        REQUIRE(!(image_resource_extension(read_resource(build_canonical(spec))).has_value()));
    }
    SECTION("a Godot 2 image of a non-image type is malformed")
    {
        resource_document_spec spec = document(2, 1);
        auto &record_ref = spec.records.emplace_back();
        record_ref.path = "res://tex";
        record_ref.type = "ImageTexture";
        record_ref.properties.push_back(
            property("image", encode(false, [](variant_encoder &encoder) { encoder.boolean(true); })));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
    }
    SECTION("a Godot 3/4 root of another class yields no image")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("res://root", "Node", {}));
        REQUIRE(!(image_resource_extension(read_resource(build_canonical(spec))).has_value()));
    }
}

TEST_CASE("godot image resource: rejects unsupported reference shapes")
{
    const bool modern = true;
    SECTION("an empty object reference yields no image")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("local://0", "Image", {}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_empty(); }))}));
        REQUIRE(!(image_resource_extension(read_resource(build_canonical(spec))).has_value()));
    }
    SECTION("an external object reference yields no image")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("local://0", "Image", {}));
        spec.records.push_back(
            record("res://tex", "ImageTexture", {property("image", encode(modern, [](variant_encoder &encoder) {
                                                              encoder.object_external("Image", "res://x.png");
                                                          }))}));
        REQUIRE(!(image_resource_extension(read_resource(build_canonical(spec))).has_value()));
    }
    SECTION("a reference to a record of another class yields no image")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("local://0", "Node", {}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        REQUIRE(!(image_resource_extension(read_resource(build_canonical(spec))).has_value()));
    }
    SECTION("write rejects an unsupported resource rather than emitting a stream")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("res://root", "Node", {}));
        REQUIRE_THROWS_AS(write_resource(read_resource(build_canonical(spec))), image_error);
    }
}

TEST_CASE("godot image resource: rejects malformed Image dictionaries")
{
    const bool modern = true;
    SECTION("a missing required field is malformed")
    {
        const auto data =
            image_data(modern, {vstring(modern, "height"), vint(modern, 1), vstring(modern, "format"),
                                vstring(modern, "RGBA8"), vstring(modern, "mipmaps"), vbool(modern, false),
                                vstring(modern, "data"), vbytes(modern, u8_list({1, 2, 3, 4}))});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("a field of the wrong type is malformed")
    {
        const auto data = image_data(
            modern, {vstring(modern, "width"), vstring(modern, "two"), vstring(modern, "height"), vint(modern, 1),
                     vstring(modern, "format"), vstring(modern, "RGBA8"), vstring(modern, "mipmaps"),
                     vbool(modern, false), vstring(modern, "data"), vbytes(modern, u8_list({1, 2, 3, 4}))});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("a non-positive dimension is malformed")
    {
        const auto data = standard_image_data(modern, 0, 1, "RGBA8", false, u8_list({1, 2, 3, 4}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("an unknown format name yields no image")
    {
        const auto data = standard_image_data(modern, 1, 1, "Nonsense", false, u8_list({1, 2, 3, 4}));
        REQUIRE(!(image_resource_extension(read_resource(modern_texture(4, 6, data))).has_value()));
    }
    SECTION("a known but unsupported format name yields no image")
    {
        const auto data = standard_image_data(modern, 1, 1, "RFloat", false, u8_list({1, 2, 3, 4}));
        REQUIRE(!(image_resource_extension(read_resource(modern_texture(4, 6, data))).has_value()));
    }
    SECTION("a breached pixel budget is malformed")
    {
        const auto data = standard_image_data(modern, 100000, 100000, "RGBA8", false, u8_list({1, 2, 3, 4}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
}

TEST_CASE("godot image resource: resolves Godot 3/4 ImageTexture and root Image resources")
{
    const bool modern3 = false;
    const bool modern4 = true;
    const auto pixels = u8_list({1, 2, 3, 4, 5, 6, 7, 8});

    SECTION("godot 3 ImageTexture through an internal Image record")
    {
        const binary_resource resource =
            read_resource(modern_texture(3, 3, standard_image_data(modern3, 2, 1, "RGBA8", false, pixels)));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        const auto decoded = decode_png(write_resource(resource));
        REQUIRE(decoded.width == 2);
        REQUIRE(decoded.height == 1);
        REQUIRE(decoded.pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
    }
    SECTION("godot 4 ImageTexture through an internal Image record")
    {
        const binary_resource resource =
            read_resource(modern_texture(4, 6, standard_image_data(modern4, 2, 1, "RGBA8", false, pixels)));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
    }
    SECTION("a root Image resource with the same verified data")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("res://image", "Image",
                                      {property("data", standard_image_data(modern4, 2, 1, "RGBA8", false, pixels))}));
        const binary_resource resource = read_resource(build_canonical(spec));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
    }
    SECTION("godot 3 RGBA5551 and godot 4 RGB565 share the format name but differ in packing")
    {
        const auto pixels5551 = u8_list({0x21, 0xF8});
        const auto pixels565 = u8_list({0x10, 0x84});
        const binary_resource g3 =
            read_resource(modern_texture(3, 3, standard_image_data(modern3, 1, 1, "RGBA5551", false, pixels5551)));
        const binary_resource g4 =
            read_resource(modern_texture(4, 6, standard_image_data(modern4, 1, 1, "RGBA5551", false, pixels565)));
        REQUIRE(decode_png(write_resource(g3)).pixels == byte_list({255, 0, 255, 255}));
        REQUIRE(decode_png(write_resource(g4)).pixels == byte_list({131, 129, 131, 255}));
    }
}

TEST_CASE("godot image resource: validates the stored mip chain and converts the base only")
{
    const bool modern = true;
    SECTION("a luminance chain uses only its first level")
    {
        // 4x4 Lum8 with mipmaps: 16 + 4 + 1 bytes.
        std::vector<std::uint8_t> chain(16, 0x7F);
        chain.insert(chain.end(), 5, 0x11);
        const auto data = standard_image_data(modern, 4, 4, "Lum8", true, chain);
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        const auto decoded = decode_png(write_resource(resource));
        REQUIRE(decoded.width == 4);
        REQUIRE(decoded.height == 4);
        for (std::size_t index = 0; index < 16; ++index) {
            REQUIRE(decoded.pixels[index * 4 + 3] == std::byte{255});
            REQUIRE(decoded.pixels[index * 4] == std::byte{0x7F});
        }
    }
    SECTION("a DXT1 chain uses the block-rounded level sizes")
    {
        // 8x8 DXT1 with mipmaps: 32 + 8 + 8 + 8 bytes.
        std::vector<std::uint8_t> chain(56, 0);
        const auto data = standard_image_data(modern, 8, 8, "DXT1 RGB8", true, chain);
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        const auto expected = unpackrat::codecs::decode_bc(
            std::span<const std::byte>(reinterpret_cast<const std::byte *>(chain.data()), 32), bc_format::bc1, 8, 8);
        REQUIRE(decode_png(write_resource(resource)).pixels == expected.pixels);
    }
    SECTION("a chain shorter than the geometry is malformed")
    {
        const auto data = standard_image_data(modern, 4, 4, "Lum8", true, std::vector<std::uint8_t>(16, 0));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("trailing bytes without mipmaps are malformed")
    {
        const auto data = standard_image_data(modern, 4, 4, "Lum8", false, std::vector<std::uint8_t>(21, 0));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("a level shorter than a single mip is malformed")
    {
        const auto data = standard_image_data(modern, 4, 4, "Lum8", false, std::vector<std::uint8_t>(8, 0));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("a single-column chain pads the halving floor")
    {
        // 1x8 Lum8 with mipmaps: 8 + 4 + 2 + 1 bytes.
        std::vector<std::uint8_t> chain(8, 0x40);
        chain.insert(chain.end(), 7, 0);
        const auto data = standard_image_data(modern, 1, 8, "Lum8", true, chain);
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        const auto decoded = decode_png(write_resource(resource));
        REQUIRE(decoded.width == 1);
        REQUIRE(decoded.height == 8);
    }
    SECTION("a single-row chain pads the halving floor")
    {
        // 8x1 Lum8 with mipmaps: 8 + 4 + 2 + 1 bytes.
        std::vector<std::uint8_t> chain(8, 0x40);
        chain.insert(chain.end(), 7, 0);
        const auto data = standard_image_data(modern, 8, 1, "Lum8", true, chain);
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
    }
    SECTION("a one-pixel chain terminates at one level")
    {
        const auto data = standard_image_data(modern, 1, 1, "Lum8", true, u8_list({0x33}));
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({0x33, 0x33, 0x33, 255}));
    }
}

TEST_CASE("godot image resource: the probe and the writer agree for every outcome")
{
    const bool modern = true;
    SECTION("an unsupported resource is reported by both")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("res://root", "Node", {}));
        const binary_resource resource = read_resource(build_canonical(spec));
        REQUIRE(!(image_resource_extension(resource).has_value()));
        REQUIRE_THROWS_AS(write_resource(resource), image_error);
    }
    SECTION("a malformed resource is rejected by both")
    {
        const auto data = standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({1, 2, 3}));
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE_THROWS_AS(image_resource_extension(resource), image_error);
        REQUIRE_THROWS_AS(write_resource(resource), image_error);
    }
    SECTION("a supported resource is reported and written by both")
    {
        const auto data = standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({9, 8, 7, 6}));
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({9, 8, 7, 6}));
    }
}

TEST_CASE("godot image resource: a referenced Image record at the directory edge")
{
    // The referenced index points at the ImageTexture itself: the class check rejects it.
    const binary_resource resource = read_resource(modern_texture_reference(4, 6, 1));
    REQUIRE(!(image_resource_extension(resource).has_value()));
}

TEST_CASE("godot image resource: a Godot 2 raw image with an unsupported format yields no image")
{
    const std::vector<std::uint8_t> data = u8_list({0, 1, 2, 3});
    REQUIRE(!(image_resource_extension(read_resource(g2_raw_texture(2, 2, 0, 5, data))).has_value()));
}

TEST_CASE("godot image resource: a Godot 2 raw image with a degenerate extent is malformed")
{
    SECTION("zero width")
    {
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(0, 2, 0, 4, u8_list({1, 2, 3, 4})))),
                          image_error);
    }
    SECTION("zero height")
    {
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 0, 0, 4, u8_list({1, 2, 3, 4})))),
                          image_error);
    }
    SECTION("a mip chain shorter than the base level is malformed")
    {
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 1, 1, 4, u8_list({1, 2, 3, 4})))),
                          image_error);
    }
}

TEST_CASE("godot image resource: rejects malformed WebP envelopes")
{
    SECTION("an inner container shorter than the header")
    {
        const std::vector<std::uint8_t> payload = u8_list({'W', 'E', 'B', 'P', 'R', 'I', 'F', 'F'});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, payload))), image_error);
    }
    SECTION("an inner container that is not RIFF")
    {
        std::vector<std::uint8_t> payload = u8_list({'W', 'E', 'B', 'P', 'X', 'I', 'F', 'F'});
        payload.insert(payload.end(), 8, 0);
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, payload))), image_error);
    }
    SECTION("an inner container whose fourcc is not WEBP")
    {
        std::vector<std::uint8_t> payload =
            u8_list({'W', 'E', 'B', 'P', 'R', 'I', 'F', 'F', 8, 0, 0, 0, 'V', 'P', '8', ' '});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, payload))), image_error);
    }
}

TEST_CASE("godot image resource: rejects malformed hand-built references and Image records")
{
    const bool modern = true;
    SECTION("an internal reference outside the directory is malformed")
    {
        binary_resource resource;
        resource.engine.major = 4;
        resource.main_type = "ImageTexture";
        resource_record texture;
        texture.type = "ImageTexture";
        resource_property image;
        image.name = "image";
        image.value.kind = value_kind::reference;
        image.value.reference.index = 9;
        texture.properties.push_back(image);
        resource.records.push_back(texture);
        REQUIRE_THROWS_AS(image_resource_extension(resource), image_error);
    }
    SECTION("an inline external reference yields no image")
    {
        binary_resource resource;
        resource.engine.major = 4;
        resource.main_type = "ImageTexture";
        resource_record texture;
        texture.type = "ImageTexture";
        resource_property image;
        image.name = "image";
        image.value.kind = value_kind::reference;
        image.value.reference.inline_definition = true;
        image.value.reference.type = "Image";
        image.value.reference.path = "res://x.png";
        texture.properties.push_back(image);
        resource.records.push_back(texture);
        REQUIRE(!(image_resource_extension(resource).has_value()));
    }
    SECTION("an Image record without a data property is malformed")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("local://0", "Image", {}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
    }
    SECTION("an Image record whose data property is not a dictionary is malformed")
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(
            record("local://0", "Image",
                   {property("data", encode(modern, [](variant_encoder &encoder) { encoder.int32(1); }))}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
    }
    SECTION("a width wider than a 32-bit field is malformed")
    {
        const auto data = image_data(
            modern, {vstring(modern, "width"), vint64(modern, 0x100000000LL), vstring(modern, "height"),
                     vint(modern, 1), vstring(modern, "format"), vstring(modern, "RGBA8"), vstring(modern, "mipmaps"),
                     vbool(modern, false), vstring(modern, "data"), vbytes(modern, u8_list({1, 2, 3, 4}))});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("a negative dimension is malformed")
    {
        const auto data = standard_image_data(modern, -1, 1, "RGBA8", false, u8_list({1, 2, 3, 4}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
}

TEST_CASE("godot image resource: enforces the decoded RGBA budget on raw descriptors")
{
    const bool modern = true;
    SECTION("a probe rejects a decoded size above the output budget")
    {
        // 512x512 RGBA decodes to 1048576 bytes, above a 1000000-byte decoded budget.
        const std::vector<std::uint8_t> pixels(std::size_t{512} * 512 * 4, 0);
        const auto data = standard_image_data(modern, 512, 512, "RGBA8", false, pixels);
        image_limits limits;
        limits.max_output_bytes = 1000000;
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data)), limits), image_error);
        std::size_t calls = 0;
        REQUIRE_THROWS_AS(
            write_image_resource(
                read_resource(modern_texture(4, 6, data)), [&](std::span<const std::byte>) { ++calls; }, limits),
            image_error);
        REQUIRE(calls == 0);
    }
    SECTION("the probe accepts a decoded size equal to the budget")
    {
        const auto data = standard_image_data(modern, 2, 1, "RGBA8", false, u8_list({1, 2, 3, 4, 5, 6, 7, 8}));
        image_limits limits;
        limits.max_output_bytes = 8;
        REQUIRE(image_resource_extension(read_resource(modern_texture(4, 6, data)), limits).value_or("") == ".png");
    }
    SECTION("a zero output budget rejects any raw image")
    {
        const auto data = standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({1, 2, 3, 4}));
        image_limits limits;
        limits.max_output_bytes = 0;
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data)), limits), image_error);
        REQUIRE_THROWS_AS(write_resource(read_resource(modern_texture(4, 6, data)), limits), image_error);
    }
    SECTION("a decoded size within the budget converts and writes the image")
    {
        const auto data = standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({9, 8, 7, 6}));
        image_limits limits;
        limits.max_output_bytes = 256;
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE(image_resource_extension(resource, limits).value_or("") == ".png");
        REQUIRE(decode_png(write_resource(resource, limits)).pixels == byte_list({9, 8, 7, 6}));
    }
}

TEST_CASE("godot image resource: the probe enforces the block-decoded output budget")
{
    // 4x4 BC1 decodes to 64 RGBA bytes; the writer already rejected it and the probe must too.
    const auto bc1 = u8_list({0xFF, 0xFF, 0x00, 0x00, 0xE4, 0x00, 0x00, 0x00});
    SECTION("one byte below the decoded size is rejected by the probe and the writer")
    {
        const auto data = standard_image_data(true, 4, 4, "DXT1 RGB8", false, bc1);
        image_limits limits;
        limits.max_output_bytes = 63;
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data)), limits), image_error);
        REQUIRE_THROWS_AS(write_resource(read_resource(modern_texture(4, 6, data)), limits), image_error);
    }
    SECTION("a BC image within both budgets converts and writes")
    {
        const auto data = standard_image_data(true, 4, 4, "DXT1 RGB8", false, bc1);
        image_limits limits;
        limits.max_output_bytes = 256;
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE(image_resource_extension(resource, limits).value_or("") == ".png");
        const auto decoded = decode_png(write_resource(resource, limits));
        REQUIRE(decoded.width == 4);
        REQUIRE(decoded.height == 4);
    }
}

TEST_CASE("godot image resource: write_raw enforces the decoded RGBA budget before the sink")
{
    SECTION("a one-byte-short budget rejects without a sink call")
    {
        const std::vector<std::byte> top(std::size_t{512} * 512 * 4, std::byte{0});
        image_limits limits;
        limits.max_output_bytes = 1000000;
        std::size_t calls = 0;
        REQUIRE_THROWS_AS(
            write_raw_image(top, 4, 5, 512, 512, [&](std::span<const std::byte>) { ++calls; }, limits), image_error);
        REQUIRE(calls == 0);
    }
    SECTION("a decoded size within the budget writes the image")
    {
        image_limits limits;
        limits.max_output_bytes = 256;
        REQUIRE(decode_png(write_raw(byte_list({9, 8, 7, 6}), 4, 5, 1, 1, limits)).pixels == byte_list({9, 8, 7, 6}));
    }
}

TEST_CASE("godot image resource: validates the exact Godot 2 additional-level chain")
{
    SECTION("one additional level adds a single minimum texel")
    {
        std::vector<std::uint8_t> data = u8_list({1, 2, 3, 4, 5, 6, 7, 8});
        data.insert(data.end(), 4, 0xFF);
        const binary_resource resource = read_resource(g2_raw_texture(2, 1, 1, 4, data));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({1, 2, 3, 4, 5, 6, 7, 8}));
    }
    SECTION("two additional levels repeat the clamped minimum")
    {
        std::vector<std::uint8_t> data = u8_list({1, 2, 3, 4, 5, 6, 7, 8});
        data.insert(data.end(), 8, 0);
        REQUIRE(image_resource_extension(read_resource(g2_raw_texture(2, 1, 2, 4, data))).value_or("") == ".png");
    }
    SECTION("a trailing byte beyond the declared chain is rejected")
    {
        std::vector<std::uint8_t> data = u8_list({1, 2, 3, 4, 5, 6, 7, 8});
        data.insert(data.end(), 5, 0);
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 1, 1, 4, data))), image_error);
    }
    SECTION("a chain missing its final level is rejected")
    {
        const std::vector<std::uint8_t> data(11, 0);
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 1, 1, 4, data))), image_error);
    }
    SECTION("a declared count over the resource cap is rejected")
    {
        const std::vector<std::uint8_t> data(8 + 33 * 4, 0);
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(2, 1, 33, 4, data))), image_error);
    }
    SECTION("the cap boundary is accepted")
    {
        std::vector<std::uint8_t> data(8, 0);
        data.insert(data.end(), std::size_t{32} * 4, 0);
        REQUIRE(image_resource_extension(read_resource(g2_raw_texture(2, 1, 32, 4, data))).value_or("") == ".png");
    }
    SECTION("a block-compressed chain uses the generation geometry")
    {
        // 4x4 BC1 base = 8 bytes; one clamped 4x4 level adds 8 more.
        const auto bc1 = u8_list({0xFF, 0xFF, 0x00, 0x00, 0xE4, 0x00, 0x00, 0x00});
        std::vector<std::uint8_t> data = bc1;
        data.insert(data.end(), 8, 0);
        REQUIRE(image_resource_extension(read_resource(g2_raw_texture(4, 4, 1, 7, data))).value_or("") == ".png");
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_raw_texture(4, 4, 1, 7, bc1))), image_error);
    }
    SECTION("a chain whose total overflows the byte count is rejected")
    {
        image_limits limits;
        limits.max_pixels = (std::numeric_limits<std::size_t>::max)();
        REQUIRE_THROWS_AS(image_resource_extension(
                              read_resource(g2_raw_texture(0xFFFFFFFFU, 0xFFFFFFFFU, 1, 4, u8_list({}))), limits),
                          image_error);
    }
}

TEST_CASE("godot image resource: enforces the output budget on preserved encoded streams")
{
    rgba_image source;
    source.width = 2;
    source.height = 1;
    source.pixels = byte_list({1, 2, 3, 4, 5, 6, 7, 8});
    std::vector<std::uint8_t> inner;
    unpackrat::codecs::encode_png(source, [&](const std::span<const std::byte> chunk) {
        for (const std::byte value : chunk) {
            inner.push_back(static_cast<std::uint8_t>(value));
        }
    });
    std::vector<std::uint8_t> envelope = u8_list({'P', 'N', 'G', ' '});
    envelope.insert(envelope.end(), inner.begin(), inner.end());
    const std::size_t length = inner.size();

    const binary_resource resource = read_resource(g2_encoded_texture(2, envelope));
    SECTION("the exact length succeeds and emits the stream unchanged")
    {
        image_limits limits;
        limits.max_output_bytes = length;
        REQUIRE(image_resource_extension(resource, limits).value_or("") == ".png");
        REQUIRE(write_resource(resource, limits) == as_byte_vector(inner));
    }
    SECTION("one byte below the preserved length is rejected by both without a sink call")
    {
        image_limits limits;
        limits.max_output_bytes = length - 1;
        REQUIRE_THROWS_AS(image_resource_extension(resource, limits), image_error);
        std::size_t calls = 0;
        REQUIRE_THROWS_AS(
            write_image_resource(resource, [&](std::span<const std::byte>) { ++calls; }, limits), image_error);
        REQUIRE(calls == 0);
    }
    SECTION("a zero budget is rejected")
    {
        image_limits limits;
        limits.max_output_bytes = 0;
        REQUIRE_THROWS_AS(image_resource_extension(resource, limits), image_error);
    }
    SECTION("a preserved WebP stream above the budget is rejected")
    {
        const std::vector<std::uint8_t> webp = u8_list({'R', 'I', 'F', 'F', 4, 0, 0, 0, 'W', 'E', 'B', 'P'});
        std::vector<std::uint8_t> webp_envelope = u8_list({'W', 'E', 'B', 'P'});
        webp_envelope.insert(webp_envelope.end(), webp.begin(), webp.end());
        const binary_resource webp_resource = read_resource(g2_encoded_texture(3, webp_envelope));
        image_limits limits;
        limits.max_output_bytes = webp.size() - 1;
        REQUIRE_THROWS_AS(image_resource_extension(webp_resource, limits), image_error);
        limits.max_output_bytes = webp.size();
        REQUIRE(image_resource_extension(webp_resource, limits).value_or("") == ".webp");
        REQUIRE(write_resource(webp_resource, limits) == as_byte_vector(webp));
    }
}

TEST_CASE("godot image resource: the header main type must match a known image root")
{
    const bool modern = true;
    SECTION("a header of another class than the root record is rejected")
    {
        resource_document_spec spec = document(4, 6);
        spec.main_type = "Image";
        spec.records.push_back(
            record("local://0", "Image",
                   {property("data", standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({1, 2, 3, 4})))}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_resource(spec))), image_error);
        REQUIRE_THROWS_AS(write_resource(read_resource(build_resource(spec))), image_error);
    }
    SECTION("a root Image with an ImageTexture header is rejected")
    {
        resource_document_spec spec = document(4, 6);
        spec.main_type = "ImageTexture";
        spec.records.push_back(
            record("res://image", "Image",
                   {property("data", standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({1, 2, 3, 4})))}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_resource(spec))), image_error);
    }
    SECTION("an unrelated class resolves to no image regardless of the header")
    {
        resource_document_spec spec = document(4, 6);
        spec.main_type = "Resource";
        spec.records.push_back(record("res://root", "Node", {}));
        REQUIRE(!(image_resource_extension(read_resource(build_resource(spec))).has_value()));
    }
}

TEST_CASE("godot image resource: validates the WebP RIFF declared size")
{
    const auto envelope = [](const std::vector<std::uint8_t> &inner) {
        std::vector<std::uint8_t> result = u8_list({'W', 'E', 'B', 'P'});
        result.insert(result.end(), inner.begin(), inner.end());
        return result;
    };
    SECTION("a zero declared size is rejected")
    {
        const auto env = envelope(u8_list({'R', 'I', 'F', 'F', 0, 0, 0, 0, 'W', 'E', 'B', 'P'}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, env))), image_error);
    }
    SECTION("a declared size below the form-type bytes is rejected")
    {
        const auto env = envelope(u8_list({'R', 'I', 'F', 'F', 2, 0, 0, 0, 'W', 'E', 'B', 'P'}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, env))), image_error);
    }
    SECTION("the minimum declared size of four is accepted")
    {
        const auto inner = u8_list({'R', 'I', 'F', 'F', 4, 0, 0, 0, 'W', 'E', 'B', 'P'});
        const binary_resource resource = read_resource(g2_encoded_texture(3, envelope(inner)));
        REQUIRE(image_resource_extension(resource).value_or("") == ".webp");
        REQUIRE(write_resource(resource) == as_byte_vector(inner));
    }
    SECTION("a declared size beyond the payload is rejected")
    {
        const auto env = envelope(u8_list({'R', 'I', 'F', 'F', 8, 0, 0, 0, 'W', 'E', 'B', 'P'}));
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(3, env))), image_error);
    }
}

TEST_CASE("godot image resource: a modern Image whose byte count overflows is rejected")
{
    const bool modern = true;
    SECTION("an RGBA byte count beyond a 64-bit total is malformed")
    {
        const auto data =
            image_data(modern, {vstring(modern, "width"), vint64(modern, 0xFFFFFFFFLL), vstring(modern, "height"),
                                vint64(modern, 0xFFFFFFFFLL), vstring(modern, "format"), vstring(modern, "RGBA8"),
                                vstring(modern, "mipmaps"), vbool(modern, false), vstring(modern, "data"),
                                vbytes(modern, u8_list({1, 2, 3, 4}))});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
    SECTION("a padded block grid beyond a 64-bit total is malformed")
    {
        const auto data =
            image_data(modern, {vstring(modern, "width"), vint64(modern, 0xFFFFFFFFLL), vstring(modern, "height"),
                                vint64(modern, 0xFFFFFFFFLL), vstring(modern, "format"), vstring(modern, "DXT1 RGB8"),
                                vstring(modern, "mipmaps"), vbool(modern, false), vstring(modern, "data"),
                                vbytes(modern, u8_list({1, 2, 3, 4}))});
        REQUIRE_THROWS_AS(image_resource_extension(read_resource(modern_texture(4, 6, data))), image_error);
    }
}

TEST_CASE("godot image resource: a Godot 2 chain halves each clamped level")
{
    // 8x8 grayscale: the base stores 64 bytes and one added level halves to a 4x4 16-byte level.
    std::vector<std::uint8_t> data(64, 0x5A);
    data.insert(data.end(), 16, 0x11);
    const binary_resource resource = read_resource(g2_raw_texture(8, 8, 1, 0, data));
    REQUIRE(image_resource_extension(resource).value_or("") == ".png");
    REQUIRE(decode_png(write_resource(resource)).width == 8);
    REQUIRE_THROWS_AS(
        image_resource_extension(read_resource(g2_raw_texture(8, 8, 1, 0, std::vector<std::uint8_t>(64, 0)))),
        image_error);
}

TEST_CASE("godot image resource: a Godot 2 running total that overflows is rejected")
{
    const binary_resource resource = read_resource(g2_raw_texture(0xFFFFFFFFU, 0xFFFFFFFFU, 1, 0, u8_list({})));
    REQUIRE_THROWS_AS(image_resource_extension(resource), image_error);
}

TEST_CASE("godot image resource: a Godot 2 ImageTexture without an image property is malformed")
{
    resource_document_spec spec = document(2, 1);
    spec.records.push_back(record("res://tex", "ImageTexture", {}));
    REQUIRE_THROWS_AS(image_resource_extension(read_resource(build_canonical(spec))), image_error);
}

TEST_CASE("godot image resource: an ImageTexture with a leading property still resolves its image")
{
    const bool modern = true;
    resource_document_spec spec = document(4, 6);
    spec.records.push_back(
        record("local://0", "Image",
               {property("data", standard_image_data(modern, 1, 1, "RGBA8", false, u8_list({1, 2, 3, 4})))}));
    spec.records.push_back(
        record("res://tex", "ImageTexture",
               {property("flags", vint(modern, 0)),
                property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
    const binary_resource resource = read_resource(build_canonical(spec));
    REQUIRE(image_resource_extension(resource).value_or("") == ".png");
    REQUIRE(decode_png(write_resource(resource)).pixels == byte_list({1, 2, 3, 4}));
}

TEST_CASE("godot image resource: a truncated preserved envelope is rejected")
{
    std::vector<std::uint8_t> payload = u8_list({'P', 'N', 'G', ' '});
    payload.insert(payload.end(), 3, 0);
    REQUIRE_THROWS_AS(image_resource_extension(read_resource(g2_encoded_texture(2, payload))), image_error);
}

TEST_CASE("godot image resource: a Godot 2 raw block edge the whole-block decoder cannot pad yields no image")
{
    // A Godot 2 raw payload stores the exact (unrounded) block extent, but the block decoder
    // consumes a padded whole-block grid. An edge that is not a whole number of blocks therefore
    // cannot be converted; the probe must report no image so a listing never promises a stream the
    // writer would always reject.
    SECTION("a 2x2 BC1 base of two bytes")
    {
        const std::vector<std::uint8_t> data = u8_list({0xAA, 0xBB});
        const binary_resource resource = read_resource(g2_raw_texture(2, 2, 0, 7, data));
        REQUIRE(!(image_resource_extension(resource).has_value()));
        REQUIRE_THROWS_AS(write_resource(resource), image_error);
    }
    SECTION("a 6x5 BC1 base of fifteen bytes")
    {
        const std::vector<std::uint8_t> data(15, 0x7E);
        const binary_resource resource = read_resource(g2_raw_texture(6, 5, 0, 7, data));
        REQUIRE(!(image_resource_extension(resource).has_value()));
        REQUIRE_THROWS_AS(write_resource(resource), image_error);
    }
}

TEST_CASE("godot image resource: preserves a supported Godot 2 raw block conversion")
{
    const std::vector<std::uint8_t> bc1 = u8_list({0xFF, 0xFF, 0x00, 0x00, 0xE4, 0x00, 0x00, 0x00});
    SECTION("an aligned 4x4 BC1 base decodes to the same pixels as the raw decoder")
    {
        const binary_resource resource = read_resource(g2_raw_texture(4, 4, 0, 7, bc1));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        const auto expected = unpackrat::codecs::decode_bc(as_byte_vector(bc1), bc_format::bc1, 4, 4);
        REQUIRE(decode_png(write_resource(resource)).pixels == expected.pixels);
    }
    SECTION("an aligned 4x8 BC2 base decodes")
    {
        const std::vector<std::uint8_t> bc2(32, 0x0F);
        const binary_resource resource = read_resource(g2_raw_texture(4, 8, 0, 8, bc2));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        const auto decoded = decode_png(write_resource(resource));
        REQUIRE(decoded.width == 4);
        REQUIRE(decoded.height == 8);
        REQUIRE(decoded.pixels == unpackrat::codecs::decode_bc(as_byte_vector(bc2), bc_format::bc2, 4, 8).pixels);
    }
}

TEST_CASE("godot image resource: the Godot 2 raw block edge decision table")
{
    struct edge_row final
    {
        std::uint32_t format;
        std::uint32_t width;
        std::uint32_t height;
        bool convertible;
    };
    // BC1 and BC4 pack two pixels per byte (a right shift of one); BC2, BC3 and BC5 store one
    // byte per texel. Only a base that is a whole number of 4x4 blocks matches the decoder.
    const edge_row rows[] = {
        {7, 4, 4, true},   // BC1 aligned
        {7, 2, 2, false},  // BC1 below one block
        {7, 6, 5, false},  // BC1 partial edge
        {8, 4, 4, true},   // BC2 aligned
        {8, 4, 8, true},   // BC2 aligned non-square
        {9, 3, 4, false},  // BC3 partial edge
        {10, 4, 4, true},  // BC4 aligned
        {10, 2, 8, false}, // BC4 narrow edge
        {11, 5, 4, false}, // BC5 partial edge
    };
    for (const edge_row &entry : rows) {
        const bool halved = entry.format == 7 || entry.format == 10;
        const std::uint32_t base_bytes = entry.width * entry.height >> (halved ? 1U : 0U);
        const std::vector<std::uint8_t> data(base_bytes, 0x33);
        const binary_resource resource =
            read_resource(g2_raw_texture(entry.width, entry.height, 0, entry.format, data));
        const std::optional<std::string_view> extension = image_resource_extension(resource);
        if (entry.convertible) {
            REQUIRE(extension.value_or("") == ".png");
            const auto decoded = decode_png(write_resource(resource));
            REQUIRE(decoded.width == entry.width);
            REQUIRE(decoded.height == entry.height);
        } else {
            REQUIRE(!(extension.has_value()));
            REQUIRE_THROWS_AS(write_resource(resource), image_error);
        }
    }
}

TEST_CASE("godot image resource: the block edge is padded by a modern Image but not a Godot 2 raw payload")
{
    // The same 6x5 extent: a Godot 3/4 Image stores the block-rounded 32-byte grid and converts,
    // while a Godot 2 raw payload stores the exact 15-byte edge and yields no image.
    SECTION("a Godot 2 exact edge yields no image")
    {
        const binary_resource resource = read_resource(g2_raw_texture(6, 5, 0, 7, std::vector<std::uint8_t>(15, 0)));
        REQUIRE(!(image_resource_extension(resource).has_value()));
    }
    SECTION("a modern block-rounded grid converts")
    {
        const std::vector<std::uint8_t> grid(32, 0x11);
        const auto data = standard_image_data(true, 6, 5, "DXT1 RGB8", false, grid);
        const binary_resource resource = read_resource(modern_texture(4, 6, data));
        REQUIRE(image_resource_extension(resource).value_or("") == ".png");
        const auto decoded = decode_png(write_resource(resource));
        REQUIRE(decoded.width == 6);
        REQUIRE(decoded.height == 5);
    }
}

TEST_CASE("godot image resource: DXT5 RA-as-RG restores the two Godot channels", "[godot][texture-media]")
{
    const auto block = byte_list({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0, 0, 0, 0x00, 0xF8, 0, 0, 0, 0, 0, 0});
    REQUIRE(supports_raw_image(4, 34));
    const auto decoded = decode_png(write_raw(block, 4, 34, 4, 4));
    const std::vector<unsigned> alpha{0, 255, 51, 102, 153, 204, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0};
    REQUIRE(decoded.pixels.size() == 64);
    for (std::size_t i = 0; i < alpha.size(); ++i) {
        CHECK(decoded.pixels[i * 4] == std::byte{255});
        CHECK(decoded.pixels[i * 4 + 1] == static_cast<std::byte>(alpha[i]));
        CHECK(decoded.pixels[i * 4 + 2] == std::byte{0});
        CHECK(decoded.pixels[i * 4 + 3] == std::byte{255});
    }
    CHECK(!(supports_raw_image(3, 34)));
}

TEST_CASE("godot image resource: default raw probe has no old pixel or RGBA byte ceiling", "[godot][texture-media]")
{
    // Input is 72 MB L8; probing must not allocate the 288 MB RGBA result or encode PNG.
    auto resource = read_resource(modern_texture(4, 6, standard_image_data(true, 1, 1, "Lum8", false, u8_list({42}))));
    REQUIRE(resource.records.size() == 2);
    auto &fields = resource.records.front().properties.front().value.dictionary;
    for (auto &field : fields) {
        if (field.name == "width")
            field.value.integer = 9000;
        if (field.name == "height")
            field.value.integer = 8000;
        if (field.name == "data")
            field.value.bytes.assign(std::size_t{9000} * 8000, std::byte{42});
    }
    CHECK(image_resource_extension(resource) == ".png");
}

TEST_CASE("godot image metadata: raw inspection uses declared byte lengths without pixel payload",
          "[godot][image-metadata]")
{
    SECTION("modern Image and ImageTexture raw mip geometry")
    {
        for (const std::uint32_t major : {3U, 4U}) {
            const std::vector<std::uint8_t> pixels(10000, 42);
            auto resource = read_resource(modern_texture(
                major, major == 4 ? 6 : 3, standard_image_data(major == 4, 100, 100, "Lum8", false, pixels)));
            auto &data = resource.records[0].properties[0].value.dictionary[4].value;
            data.byte_length = data.bytes.size();
            data.bytes.resize(64);
            const auto info = unpackrat::godot::inspect_image_resource_metadata(resource);
            REQUIRE(info.has_value());
            if (!info.has_value()) {
                return;
            }
            CHECK(info.value().extension == ".png");
            CHECK(info.value().decode_bytes == 40000);
            CHECK(data.bytes.size() == 64);
            CHECK_THROWS_AS(image_resource_extension(resource), image_error);
        }
    }
    SECTION("legacy embedded raw geometry")
    {
        const std::vector<std::uint8_t> pixels(128, 42);
        auto resource = read_resource(g2_raw_texture(128, 1, 0, 0, pixels));
        auto &image = resource.records.back().properties[0].value.image;
        image.byte_length = image.data.size();
        image.data.resize(64);
        const auto info = unpackrat::godot::inspect_image_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        CHECK(info.value().extension == ".png");
        CHECK(info.value().decode_bytes == 512);
        CHECK_THROWS_AS(image_resource_extension(resource), image_error);
    }
}

TEST_CASE("godot image metadata: preserved signatures use full declared lengths", "[godot][image-metadata]")
{
    for (const bool webp : {false, true}) {
        std::vector<std::uint8_t> payload =
            webp ? u8_list({'W', 'E', 'B', 'P', 'R', 'I', 'F', 'F', 116, 0, 0, 0, 'W', 'E', 'B', 'P'})
                 : u8_list({'P', 'N', 'G', ' ', 0x89, 'P', 'N', 'G', 13, 10, 26, 10});
        payload.resize(128);
        auto resource = read_resource(g2_encoded_texture(webp ? 3 : 2, payload));
        auto &image = resource.records.back().properties[0].value.image;
        image.byte_length = image.data.size();
        image.data.resize(64);
        const auto info = unpackrat::godot::inspect_image_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        CHECK(info.value().extension == (webp ? ".webp" : ".png"));
        CHECK(info.value().output_size == 124);
        CHECK(info.value().decode_bytes == 0);
        image_limits limits;
        limits.max_output_bytes = 100;
        CHECK_THROWS_AS(unpackrat::godot::inspect_image_resource_metadata(resource, limits), image_error);
    }
}

TEST_CASE("godot image metadata: invalid declared extents and truncated prefixes are rejected",
          "[godot][image-metadata]")
{
    const std::vector<std::uint8_t> pixels(128, 42);
    auto resource = read_resource(g2_raw_texture(128, 1, 0, 0, pixels));
    auto &image = resource.records.back().properties[0].value.image;
    image.byte_length = 128;
    image.data.resize(64);
    SECTION("declared media cannot be shorter than the retained prefix")
    {
        image.byte_length = 63;
        CHECK_THROWS_AS(unpackrat::godot::inspect_image_resource_metadata(resource), image_error);
    }
    SECTION("raw declared length must match its mip chain")
    {
        image.byte_length = 127;
        CHECK_THROWS_AS(unpackrat::godot::inspect_image_resource_metadata(resource), image_error);
    }
    SECTION("a preserved PNG requires its complete signature in the prefix")
    {
        image.encoding = 2;
        image.data = byte_list({'P', 'N', 'G', ' ', 0x89, 'P', 'N', 'G'});
        CHECK_THROWS_AS(unpackrat::godot::inspect_image_resource_metadata(resource), image_error);
    }
    SECTION("a preserved WebP requires its complete RIFF header in the prefix")
    {
        image.encoding = 3;
        image.data = byte_list({'W', 'E', 'B', 'P', 'R', 'I', 'F', 'F', 116, 0, 0, 0});
        CHECK_THROWS_AS(unpackrat::godot::inspect_image_resource_metadata(resource), image_error);
    }
}

TEST_CASE("godot image resource: named RA-as-RG images use the modern channel mapping", "[godot][image-name34]")
{
    const auto block = u8_list({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0, 0, 0, 0x00, 0xF8, 0, 0, 0, 0, 0, 0});
    const auto resource =
        read_resource(modern_texture(4, 6, standard_image_data(true, 4, 4, "FORMAT_DXT5_RA_AS_RG", false, block)));
    REQUIRE(image_resource_extension(resource).value_or("") == ".png");
    const auto pixels = decode_png(write_resource(resource)).pixels;
    CHECK(pixels[0] == std::byte{255});
    CHECK(pixels[1] == std::byte{0});
    CHECK(pixels[2] == std::byte{0});
    CHECK(pixels[3] == std::byte{255});
    CHECK(pixels[5] == std::byte{255});
}

TEST_CASE("godot image streaming: raw pixels are read by rows and compressed blocks", "[godot][image-streaming]")
{
    for (const std::uint32_t format : {0U, 5U, 17U, 34U}) {
        const std::uint32_t width = 6, height = 5;
        const std::size_t unit = format == 17 ? 8 : 16;
        auto input = format >= 17 ? std::vector<std::byte>(4 * unit, std::byte{0})
                                  : std::vector<std::byte>(std::size_t{30} * (format == 0 ? 1 : 4), std::byte{42});
        const auto expected = decode_png(write_raw(input, 4, format, width, height));
        std::size_t reads = 0, max_read = 0;
        const std::size_t row_limit = format >= 17 ? 2 * unit : static_cast<std::size_t>(width) * (format == 0 ? 1 : 4);
        unpackrat::io::byte_source source{
            input.size(), [&](const std::uint64_t offset, const std::span<std::byte> bytes) {
                if (bytes.size() > row_limit)
                    throw std::runtime_error("whole image read is prohibited");
                REQUIRE(offset <= input.size());
                REQUIRE(bytes.size() <= input.size() - offset);
                std::copy_n(input.data() + static_cast<std::size_t>(offset), bytes.size(), bytes.data());
                ++reads;
                max_read = (std::max)(max_read, bytes.size());
            }};
        std::vector<std::byte> output;
        unpackrat::godot::write_raw_image_streaming(
            source, 4, format, width, height,
            [&](const std::span<const std::byte> bytes) { output.insert(output.end(), bytes.begin(), bytes.end()); });
        const auto actual = decode_png(output);
        CHECK(actual.pixels == expected.pixels);
        CHECK(max_read <= row_limit);
        CHECK(reads > 1);
    }
}

TEST_CASE("godot image streaming: raw RGBA geometry rejects a 64-bit byte overflow before source reads",
          "[godot][image-streaming][bounds]")
{
    std::size_t reads = 0;
    const unpackrat::io::byte_source source{0, [&](std::uint64_t, std::span<std::byte>) { ++reads; }};
    std::string diagnostic;
    try {
        unpackrat::godot::write_raw_image_streaming(source, 4, 5, 0xFFFFFFFFU, 0xFFFFFFFFU,
                                                    [](std::span<const std::byte>) {});
    } catch (const unpackrat::codecs::image_error &error) {
        diagnostic = error.what();
    }
    CHECK(diagnostic == "decoded image byte count overflows");
    CHECK(reads == 0);
}

#ifndef _WIN64
TEST_CASE("godot image streaming: a logical raw row beyond x86 address space fails before source reads",
          "[godot][image-streaming][bounds]")
{
    constexpr std::uint32_t width = 1U << 30;
    constexpr std::uint64_t row_bytes = static_cast<std::uint64_t>(width) * 4;
    std::size_t reads = 0;
    const unpackrat::io::byte_source source{row_bytes, [&](std::uint64_t, std::span<std::byte>) { ++reads; }};
    std::string diagnostic;
    try {
        unpackrat::godot::write_raw_image_streaming(source, 4, 5, width, 1, [](std::span<const std::byte>) {});
    } catch (const unpackrat::codecs::image_error &error) {
        diagnostic = error.what();
    }
    CHECK(diagnostic == "image source row is not addressable");
    CHECK(reads == 0);
}
#endif

TEST_CASE("godot image streaming: binary raw and preserved payloads use logical offsets", "[godot][image-streaming]")
{
    for (const bool raw : {false, true}) {
        const std::vector<std::uint8_t> pixels(128, 42);
        auto envelope = u8_list({'P', 'N', 'G', ' ', 0x89, 'P', 'N', 'G', 13, 10, 26, 10});
        envelope.resize(16384, 42);
        const auto packed = raw ? g2_raw_texture(128, 1, 0, 0, pixels) : g2_encoded_texture(2, envelope);
        auto source = unpackrat::io::byte_source{
            packed.size(), [&](const std::uint64_t offset, const std::span<std::byte> bytes) {
                REQUIRE(bytes.size() <= 4096);
                REQUIRE(offset <= packed.size());
                REQUIRE(bytes.size() <= packed.size() - offset);
                for (std::size_t i = 0; i < bytes.size(); ++i)
                    bytes[i] = static_cast<std::byte>(packed[static_cast<std::size_t>(offset) + i]);
            }};
        const auto metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
        REQUIRE(metadata.records.back().properties[0].value.image.data.size() <= 64);
        std::vector<std::byte> output;
        unpackrat::godot::write_image_resource_streaming(metadata, source, [&](const std::span<const std::byte> bytes) {
            output.insert(output.end(), bytes.begin(), bytes.end());
        });
        if (raw)
            CHECK(decode_png(output).pixels == decode_png(write_raw(as_byte_vector(pixels), 2, 0, 128, 1)).pixels);
        else
            CHECK(output == as_byte_vector(std::span<const std::uint8_t>(envelope).subspan(4)));
    }
}

TEST_CASE("godot image streaming: modern pixels and legacy WebP preserve independent outputs",
          "[godot][image-streaming]")
{
    for (const bool webp : {false, true}) {
        const std::vector<std::uint8_t> pixels(128, 42);
        auto envelope = u8_list({'W', 'E', 'B', 'P', 'R', 'I', 'F', 'F', 124, 0, 0, 0, 'W', 'E', 'B', 'P'});
        envelope.resize(136, 42);
        const auto packed = webp ? g2_encoded_texture(3, envelope)
                                 : modern_texture(4, 6, standard_image_data(true, 16, 8, "Lum8", false, pixels));
        const unpackrat::io::byte_source source{
            packed.size(), [&](const std::uint64_t offset, const std::span<std::byte> bytes) {
                REQUIRE(offset <= packed.size());
                REQUIRE(bytes.size() <= packed.size() - offset);
                for (std::size_t i = 0; i < bytes.size(); ++i)
                    bytes[i] = static_cast<std::byte>(packed[static_cast<std::size_t>(offset) + i]);
            }};
        const auto metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
        std::vector<std::byte> output;
        unpackrat::godot::write_image_resource_streaming(metadata, source, [&](const std::span<const std::byte> bytes) {
            output.insert(output.end(), bytes.begin(), bytes.end());
        });
        if (webp)
            CHECK(output == as_byte_vector(std::span<const std::uint8_t>(envelope).subspan(4)));
        else {
            const auto decoded = decode_png(output);
            CHECK(decoded.width == 16);
            CHECK(decoded.height == 8);
            const auto expected = byte_list({42, 42, 42, 255});
            for (std::size_t i = 0; i < decoded.pixels.size(); ++i)
                CHECK(decoded.pixels[i] == expected[i % 4]);
        }
    }
}

TEST_CASE("godot image streaming: logical payload bounds are checked before output", "[godot][image-streaming]")
{
    const auto packed = g2_raw_texture(128, 1, 0, 0, std::vector<std::uint8_t>(128, 42));
    unpackrat::io::byte_source source{packed.size(), [&](const std::uint64_t offset, const std::span<std::byte> bytes) {
                                          for (std::size_t i = 0; i < bytes.size(); ++i)
                                              bytes[i] =
                                                  static_cast<std::byte>(packed[static_cast<std::size_t>(offset) + i]);
                                      }};
    auto metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
    auto &image = metadata.records.back().properties[0].value.image;
    SECTION("payload start beyond source")
    {
        image.byte_offset = source.size + 1;
    }
    SECTION("payload tail beyond source")
    {
        source.size = image.byte_offset + image.byte_length - 1;
    }
    SECTION("unsupported class")
    {
        metadata.main_type = "Resource";
        metadata.records.back().type = "Resource";
    }
    std::size_t writes = 0;
    CHECK_THROWS_AS(unpackrat::godot::write_image_resource_streaming(metadata, source,
                                                                     [&](std::span<const std::byte>) { ++writes; }),
                    image_error);
    CHECK(writes == 0);
}

TEST_CASE("godot image streaming: source callback failure stops extraction", "[godot][image-streaming]")
{
    struct source_failure final : std::runtime_error
    {
        using std::runtime_error::runtime_error;
    };
    const unpackrat::io::byte_source source{
        16, [](std::uint64_t, std::span<std::byte>) { throw source_failure("source"); }};
    CHECK_THROWS_AS(unpackrat::godot::write_raw_image_streaming(source, 4, 5, 2, 2, [](std::span<const std::byte>) {}),
                    source_failure);
}
TEST_CASE("godot image resource: class admission recognizes Image and ImageTexture without engine policy",
          "[godot][image][class-admission]")
{
    const std::pair<std::string_view, bool> cases[] = {
        {"Image", true},          {"ImageTexture", true}, {"image", false},
        {"ImageTexture2", false}, {"FontFile", false},    {"", false},
    };
    for (const auto &[class_name, expected] : cases) {
        CAPTURE(class_name);
        CHECK(unpackrat::godot::supports_image_resource_class(class_name) == expected);
    }

    binary_resource godot2_root_image;
    godot2_root_image.engine.major = 2;
    godot2_root_image.main_type = "Image";
    godot2_root_image.records.emplace_back();
    godot2_root_image.records.back().type = "Image";
    CHECK(!unpackrat::godot::inspect_image_resource(godot2_root_image).has_value());
}
