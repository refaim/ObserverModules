#include "../../core/godot/binary_resource.h"
#include "../support/godot_resource_fixtures.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using test::support::build_resource;
    using test::support::godot_resource_bytes;
    using test::support::godot_tag_array;
    using test::support::godot_tag_dictionary;
    using test::support::godot_tag_image;
    using test::support::godot_tag_input_event;
    using test::support::godot_tag_int;
    using test::support::godot_tag_node_path;
    using test::support::godot_tag_object;
    using test::support::godot_tag_string;
    using test::support::godot_tag_string_name;
    using test::support::resource_document_spec;
    using test::support::resource_property_spec;
    using test::support::resource_record_spec;
    using test::support::variant_encoder;
    using test::support::wire_writer;

    using unpackrat::godot::binary_resource;
    using unpackrat::godot::read_binary_resource;
    using unpackrat::godot::resource_error;
    using unpackrat::godot::resource_limits;
    using unpackrat::godot::resource_property;
    using unpackrat::godot::resource_value;
    using unpackrat::godot::value_kind;

    std::span<const std::byte> as_bytes(const godot_resource_bytes &bytes)
    {
        return {reinterpret_cast<const std::byte *>(bytes.data()), bytes.size()};
    }

    binary_resource read(const godot_resource_bytes &bytes, const bool has_magic = true,
                         const resource_limits &limits = {})
    {
        return read_binary_resource(as_bytes(bytes), has_magic, limits);
    }

    template <typename Encode>
    godot_resource_bytes encode_value(const bool modern, Encode encode, const bool real64 = false,
                                      const bool big_endian = false)
    {
        wire_writer out(big_endian, real64);
        variant_encoder encoder(out, modern, real64);
        encode(encoder);
        return out.take();
    }

    resource_document_spec base_document(const std::uint32_t major, const std::uint32_t format)
    {
        resource_document_spec spec;
        spec.engine_major = major;
        spec.engine_minor = major == 4 ? 5U : 3U;
        spec.format_version = format;
        return spec;
    }

    // A one-record document whose single property carries ``value``.
    resource_document_spec single_property(const std::uint32_t major, const std::uint32_t format,
                                           const std::string &name, godot_resource_bytes value,
                                           const std::string &type = "Resource")
    {
        resource_document_spec spec = base_document(major, format);
        spec.string_table.push_back(name);
        resource_record_spec record;
        record.path = "res://root";
        record.type = type;
        resource_property_spec property;
        property.name = name;
        property.value = std::move(value);
        record.properties.push_back(std::move(property));
        spec.records.push_back(std::move(record));
        return spec;
    }

    std::string bytes_text(const std::span<const std::byte> bytes)
    {
        return std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    }

    godot_resource_bytes raw_string(const std::string &content)
    {
        wire_writer out;
        out.u32(godot_tag_string).u32(static_cast<std::uint32_t>(content.size() + 1));
        out.raw(std::span<const std::uint8_t>(reinterpret_cast<const std::uint8_t *>(content.data()), content.size()));
        out.u8(0);
        return out.take();
    }
} // namespace

TEST_CASE("godot binary resource: reads a minimal valid file for each engine generation")
{
    for (const auto &[major, format] : {std::pair<std::uint32_t, std::uint32_t>{2, 1}, {3, 3}, {4, 6}}) {
        SECTION(std::string("godot ") + std::to_string(major))
        {
            const godot_resource_bytes bytes = build_resource(single_property(
                major, format, "value", encode_value(major >= 4, [](variant_encoder &e) { e.int32(1234); })));
            const binary_resource resource = read(bytes);
            CHECK(resource.engine.major == major);
            CHECK(resource.engine.minor == (major == 4 ? 5U : 3U));
            CHECK(resource.format_version == format);
            CHECK(resource.main_type == "Resource");
            REQUIRE(resource.records.size() == 1);
            CHECK(resource.records.front().path == "res://root");
            CHECK(resource.records.front().type == "Resource");
            REQUIRE(resource.records.front().properties.size() == 1);
            CHECK(resource.records.front().properties.front().name == "value");
            CHECK(resource.records.front().properties.front().value.kind == value_kind::integer);
            CHECK(resource.records.front().properties.front().value.integer == 1234);
        }
    }
}

TEST_CASE("godot binary resource: honours the wire endianness and real width")
{
    SECTION("little-endian 32-bit reals")
    {
        const godot_resource_bytes bytes = build_resource(
            single_property(3, 3, "f", encode_value(false, [](variant_encoder &e) { e.real_value(1.5); })));
        const binary_resource resource = read(bytes);
        REQUIRE(resource.records.size() == 1);
        CHECK(resource.records.front().properties.front().value.kind == value_kind::real);
        CHECK(resource.records.front().properties.front().value.real == 1.5);
    }
    SECTION("big-endian 32-bit reals")
    {
        resource_document_spec spec = single_property(
            3, 3, "f", encode_value(false, [](variant_encoder &e) { e.real_value(2.25); }, false, true));
        spec.big_endian = true;
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.records.front().properties.front().value.real == 2.25);
    }
    SECTION("legacy 64-bit reals from the header flag")
    {
        resource_document_spec spec =
            single_property(3, 3, "f", encode_value(false, [](variant_encoder &e) { e.real_value(3.75); }, true));
        spec.use_real64 = true;
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.records.front().properties.front().value.real == 3.75);
    }
    SECTION("modern 64-bit reals from the format flag")
    {
        resource_document_spec spec =
            single_property(4, 6, "f", encode_value(true, [](variant_encoder &e) { e.real_value(4.5); }, true));
        spec.real64_flag = true;
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.records.front().properties.front().value.real == 4.5);
    }
    SECTION("double values are always 64-bit")
    {
        const godot_resource_bytes bytes = build_resource(
            single_property(3, 3, "d", encode_value(false, [](variant_encoder &e) { e.double_value(6.125); })));
        const binary_resource resource = read(bytes);
        CHECK(resource.records.front().properties.front().value.kind == value_kind::real);
        CHECK(resource.records.front().properties.front().value.real == 6.125);
    }
}

TEST_CASE("godot binary resource: decodes every required value kind")
{
    const godot_resource_bytes nil_value = encode_value(true, [](variant_encoder &e) { e.nil(); });
    const godot_resource_bytes bool_value = encode_value(true, [](variant_encoder &e) { e.boolean(true); });
    const godot_resource_bytes int_value = encode_value(true, [](variant_encoder &e) { e.int32(-7); });
    const godot_resource_bytes int64_value = encode_value(true, [](variant_encoder &e) { e.int64(-9'000'000'000LL); });
    const godot_resource_bytes real_value = encode_value(true, [](variant_encoder &e) { e.real_value(1.0); });
    const godot_resource_bytes string_value = encode_value(true, [](variant_encoder &e) { e.string("hello"); });
    const godot_resource_bytes bytes_value =
        encode_value(true, [](variant_encoder &e) { e.bytes(std::vector<std::uint8_t>{1, 2, 3}); });
    const godot_resource_bytes int_array_value =
        encode_value(true, [](variant_encoder &e) { e.int32_array(std::vector<std::int32_t>{1, -2, 3}); });
    const godot_resource_bytes int64_array_value =
        encode_value(true, [](variant_encoder &e) { e.int64_array(std::vector<std::int64_t>{4, -5}); });
    const godot_resource_bytes real_array_value =
        encode_value(true, [](variant_encoder &e) { e.real_array(std::vector<double>{0.5, 1.5}); });
    const godot_resource_bytes float64_array_value =
        encode_value(true, [](variant_encoder &e) { e.float64_array(std::vector<double>{2.5}); });
    const godot_resource_bytes string_array_value =
        encode_value(true, [](variant_encoder &e) { e.string_array(std::vector<std::string>{"a", "b"}); });
    const godot_resource_bytes nested_value = encode_value(true, [](variant_encoder &e) {
        e.array(std::vector<godot_resource_bytes>{
            encode_value(true, [](variant_encoder &inner) { inner.int32(1); }),
            encode_value(true, [](variant_encoder &inner) { inner.string("x"); }),
        });
    });

    const auto property = [](const std::string &name, const godot_resource_bytes &value) {
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back(name);
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        resource_property_spec entry;
        entry.name = name;
        entry.value = value;
        record.properties.push_back(std::move(entry));
        spec.records.push_back(std::move(record));
        return read(build_resource(spec)).records.front().properties.front().value;
    };

    SECTION("nil")
    {
        CHECK(property("p", nil_value).kind == value_kind::nil);
    }
    SECTION("bool")
    {
        const auto value = property("p", bool_value);
        CHECK(value.kind == value_kind::boolean);
        CHECK(value.boolean);
    }
    SECTION("int32")
    {
        const auto value = property("p", int_value);
        CHECK(value.kind == value_kind::integer);
        CHECK(value.integer == -7);
    }
    SECTION("int64")
    {
        CHECK(property("p", int64_value).integer == -9'000'000'000LL);
    }
    SECTION("real")
    {
        CHECK(property("p", real_value).real == 1.0);
    }
    SECTION("string")
    {
        const auto value = property("p", string_value);
        CHECK(value.kind == value_kind::string);
        CHECK(value.string == "hello");
    }
    SECTION("packed byte array")
    {
        const auto value = property("p", bytes_value);
        CHECK(value.kind == value_kind::bytes);
        CHECK(value.bytes.size() == 3);
        CHECK(bytes_text(value.bytes) == std::string("\x01\x02\x03", 3));
    }
    SECTION("packed int32 array")
    {
        const auto value = property("p", int_array_value);
        CHECK(value.kind == value_kind::integer_array);
        CHECK(value.integer_array == std::vector<std::int64_t>{1, -2, 3});
    }
    SECTION("packed int64 array")
    {
        CHECK(property("p", int64_array_value).integer_array == std::vector<std::int64_t>{4, -5});
    }
    SECTION("packed float32 array")
    {
        CHECK(property("p", real_array_value).real_array == std::vector<double>{0.5, 1.5});
    }
    SECTION("packed float64 array")
    {
        CHECK(property("p", float64_array_value).real_array == std::vector<double>{2.5});
    }
    SECTION("packed string array")
    {
        const auto value = property("p", string_array_value);
        REQUIRE(value.kind == value_kind::array);
        REQUIRE(value.array.size() == 2);
        CHECK(value.array.at(0).string == "a");
        CHECK(value.array.at(1).string == "b");
    }
    SECTION("nested array")
    {
        const auto value = property("p", nested_value);
        REQUIRE(value.kind == value_kind::array);
        REQUIRE(value.array.size() == 2);
        CHECK(value.array.at(0).integer == 1);
        CHECK(value.array.at(1).string == "x");
    }
    SECTION("legacy real array keeps the wire real width")
    {
        const godot_resource_bytes legacy =
            encode_value(false, [](variant_encoder &e) { e.real_array(std::vector<double>{1.5, 2.5}); });
        resource_document_spec spec = single_property(2, 1, "p", legacy);
        const auto value = read(build_resource(spec)).records.front().properties.front().value;
        CHECK(value.kind == value_kind::real_array);
        CHECK(value.real_array == std::vector<double>{1.5, 2.5});
    }
    SECTION("packed byte array padding to four bytes")
    {
        const godot_resource_bytes value =
            encode_value(true, [](variant_encoder &e) { e.bytes(std::vector<std::uint8_t>{9}); });
        CHECK(property("p", value).bytes.size() == 1);
    }
}

TEST_CASE("godot binary resource: preserves and validates resource references")
{
    SECTION("empty object reference")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.object_empty(); });
        const auto decoded =
            read(build_resource(single_property(4, 6, "p", value))).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::reference);
        CHECK(decoded.reference.empty);
    }
    SECTION("legacy internal reference resolves through the local:// subresource id")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back("child");
        resource_record_spec child;
        // A sparse subresource id: the child sits at directory index 0 but carries id 1.
        child.path = "local://1";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "child";
        property.value = encode_value(true, [](variant_encoder &e) { e.object_internal(1); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(resource.records.size() == 2);
        const auto &decoded = resource.records.back().properties.front().value;
        REQUIRE(decoded.kind == value_kind::reference);
        CHECK(!(decoded.reference.external));
        // The wire id 1 must resolve to the child at directory index 0, not to index 1.
        CHECK(decoded.reference.index == 0);
    }
    SECTION("godot 3 legacy internal reference resolves through the local:// id")
    {
        resource_document_spec spec = base_document(3, 3);
        spec.string_table.push_back("child");
        resource_record_spec child;
        child.path = "local://7";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "child";
        property.value = encode_value(false, [](variant_encoder &e) { e.object_internal(7); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(resource.records.size() == 2);
        const auto &decoded = resource.records.back().properties.front().value;
        REQUIRE(decoded.kind == value_kind::reference);
        CHECK(decoded.reference.index == 0);
    }
    SECTION("godot 3 signed legacy subresource id resolves through its wire bits")
    {
        resource_document_spec spec = base_document(3, 3);
        spec.string_table.push_back("child");
        resource_record_spec child;
        child.path = "local://-1";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "child";
        property.value = encode_value(false, [](variant_encoder &e) { e.object_internal(0xFFFFFFFFU); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        const auto parsed = read(build_resource(spec));
        REQUIRE(parsed.records.size() == 2);
        CHECK(parsed.records.back().properties.front().value.reference.index == 0);
        CHECK(parsed.records.back().properties.front().value.reference.legacy_id == 0xFFFFFFFFU);
    }
    SECTION("named scene ids make an internal reference positional")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.named_scene_ids = true;
        spec.string_table.push_back("child");
        resource_record_spec child;
        // Named ids are opaque (non-numeric) tokens; the wire field is a directory index.
        child.path = "local://Child_abc123";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "child";
        property.value = encode_value(true, [](variant_encoder &e) { e.object_internal(0); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(resource.records.size() == 2);
        const auto &decoded = resource.records.back().properties.front().value;
        REQUIRE(decoded.kind == value_kind::reference);
        CHECK(decoded.reference.index == 0);
    }
    SECTION("named scene ids reject an internal index out of range")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.named_scene_ids = true;
        spec.string_table.push_back("child");
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "child";
        property.value = encode_value(true, [](variant_encoder &e) { e.object_internal(5); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(root));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("legacy internal reference rejects an unknown id")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back("child");
        resource_record_spec child;
        child.path = "local://1";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "child";
        property.value = encode_value(true, [](variant_encoder &e) { e.object_internal(9); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("duplicate legacy subresource ids are rejected")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back("child");
        resource_record_spec first;
        first.path = "local://1";
        first.type = "Child";
        resource_record_spec second;
        second.path = "local://1";
        second.type = "Child";
        spec.records.push_back(std::move(first));
        spec.records.push_back(std::move(second));
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        spec.records.push_back(std::move(root));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("malformed legacy subresource id is rejected")
    {
        resource_document_spec spec = base_document(4, 6);
        resource_record_spec child;
        child.path = "local://not-a-number";
        child.type = "Child";
        spec.records.push_back(std::move(child));
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        spec.records.push_back(std::move(root));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("external index reference")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.external_resources.push_back({"Texture", "res://ext.png"});
        spec.string_table.push_back("p");
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "p";
        property.value = encode_value(true, [](variant_encoder &e) { e.object_external_index(0); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(root));
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(resource.external_resources.size() == 1);
        CHECK(resource.external_resources.front().path == "res://ext.png");
        const auto &decoded = resource.records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::reference);
        CHECK(decoded.reference.external);
        CHECK(decoded.reference.index == 0);
    }
    SECTION("legacy inline external reference")
    {
        const godot_resource_bytes value =
            encode_value(false, [](variant_encoder &e) { e.object_external("Texture", "res://inline.png"); });
        const auto decoded =
            read(build_resource(single_property(3, 3, "p", value))).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::reference);
        CHECK(decoded.reference.external);
        CHECK(decoded.reference.inline_definition);
        CHECK(decoded.reference.type == "Texture");
        CHECK(decoded.reference.path == "res://inline.png");
    }
    SECTION("internal index out of range")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.object_internal(5); });
        CHECK_THROWS_AS(read(build_resource(single_property(4, 6, "p", value))), resource_error);
    }
    SECTION("external index out of range")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.object_external_index(3); });
        CHECK_THROWS_AS(read(build_resource(single_property(4, 6, "p", value))), resource_error);
    }
    SECTION("unknown object form")
    {
        wire_writer out;
        out.u32(godot_tag_object).u32(9);
        CHECK_THROWS_AS(read(build_resource(single_property(4, 6, "p", out.take()))), resource_error);
    }
}

TEST_CASE("godot binary resource: reads the legacy embedded image variant")
{
    SECTION("godot 2 raw image")
    {
        const godot_resource_bytes value = encode_value(
            false, [](variant_encoder &e) { e.image_raw(4, 2, 0, 4, std::vector<std::uint8_t>{1, 2, 3}); });
        const auto decoded =
            read(build_resource(single_property(2, 1, "image", value))).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::embedded_image);
        CHECK(decoded.image.encoding == 1);
        CHECK(decoded.image.width == 4);
        CHECK(decoded.image.height == 2);
        CHECK(decoded.image.format == 4);
        CHECK(bytes_text(decoded.image.data) == std::string("\x01\x02\x03", 3));
    }
    SECTION("godot 3 raw image with a versioned format field")
    {
        const godot_resource_bytes value = encode_value(
            false, [](variant_encoder &e) { e.image_raw(1, 1, 0, 4, std::vector<std::uint8_t>{7, 7, 7, 7}); });
        const auto decoded =
            read(build_resource(single_property(3, 3, "image", value))).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::embedded_image);
        CHECK(decoded.image.format == 4);
    }
    SECTION("godot 3 raw image rejects a future format version")
    {
        const godot_resource_bytes value = encode_value(
            false, [](variant_encoder &e) { e.image_raw(1, 1, 0, (1U << 24U) | 4U, std::vector<std::uint8_t>{1}); });
        CHECK_THROWS_AS(read(build_resource(single_property(3, 3, "image", value))), resource_error);
    }
    SECTION("encoded lossless image")
    {
        const godot_resource_bytes value =
            encode_value(false, [](variant_encoder &e) { e.image_encoded(2, std::vector<std::uint8_t>{9, 9}); });
        const auto decoded =
            read(build_resource(single_property(2, 1, "image", value))).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::embedded_image);
        CHECK(decoded.image.encoding == 2);
        CHECK(decoded.image.data.size() == 2);
    }
    SECTION("encoded lossy image")
    {
        const godot_resource_bytes value =
            encode_value(false, [](variant_encoder &e) { e.image_encoded(3, std::vector<std::uint8_t>{5}); });
        CHECK(read(build_resource(single_property(2, 1, "image", value)))
                  .records.front()
                  .properties.front()
                  .value.image.encoding == 3);
    }
    SECTION("empty image encoding decodes to nil")
    {
        wire_writer out;
        out.u32(godot_tag_image).u32(0);
        const auto decoded =
            read(build_resource(single_property(2, 1, "image", out.take()))).records.front().properties.front().value;
        CHECK(decoded.kind == value_kind::nil);
    }
}

TEST_CASE("godot binary resource: parses the logical payload of a compressed container")
{
    SECTION("logical payload without the magic parses with has_magic false")
    {
        resource_document_spec spec =
            single_property(4, 6, "value", encode_value(true, [](variant_encoder &e) { e.int32(42); }));
        spec.include_magic = false;
        const godot_resource_bytes logical = build_resource(spec);
        const binary_resource resource = read(logical, false);
        REQUIRE(resource.records.size() == 1);
        CHECK(resource.records.front().properties.front().value.integer == 42);
    }
    SECTION("physical and logical layouts agree on the records")
    {
        resource_document_spec physical =
            single_property(4, 6, "value", encode_value(true, [](variant_encoder &e) { e.int32(7); }));
        resource_document_spec logical = physical;
        logical.include_magic = false;
        const binary_resource with_magic = read(build_resource(physical), true);
        const binary_resource without_magic = read(build_resource(logical), false);
        CHECK(with_magic.records.front().type == without_magic.records.front().type);
        CHECK(with_magic.records.front().properties.front().value.integer ==
              without_magic.records.front().properties.front().value.integer);
    }
    SECTION("a magic-less payload read as physical is rejected")
    {
        resource_document_spec spec =
            single_property(4, 6, "value", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        spec.include_magic = false;
        CHECK_THROWS_AS(read(build_resource(spec), true), resource_error);
    }
    SECTION("a compressed RSCC container is rejected by this reader")
    {
        const godot_resource_bytes bytes =
            build_resource(single_property(4, 6, "value", encode_value(true, [](variant_encoder &e) { e.int32(1); })));
        godot_resource_bytes compressed = bytes;
        compressed.at(2) = static_cast<std::uint8_t>('C');
        compressed.at(3) = static_cast<std::uint8_t>('C');
        CHECK_THROWS_AS(read(compressed), resource_error);
    }
}

TEST_CASE("godot binary resource: reads the modern header variants")
{
    SECTION("external resource table with UIDs")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.external_resources.push_back({"Texture", "res://ext.png"});
        spec.modern_external_uids = true;
        spec.string_table.push_back("p");
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "p";
        property.value = encode_value(true, [](variant_encoder &e) { e.nil(); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(root));
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(resource.external_resources.size() == 1);
        CHECK(resource.external_resources.front().type == "Texture");
    }
    SECTION("script class header field")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.nil(); }));
        spec.has_script_class = true;
        spec.script_class = "MyClass";
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.main_type == "Resource");
        CHECK(resource.records.size() == 1);
    }
    SECTION("big-endian modern header")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int64(11); }, false, true));
        spec.big_endian = true;
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.records.front().properties.front().value.integer == 11);
    }
}

TEST_CASE("godot binary resource: structurally skips unmodelled values")
{
    const auto value_of = [](const godot_resource_bytes &value) {
        return read(build_resource(single_property(4, 6, "p", value))).records.front().properties.front().value;
    };

    SECTION("fixed-size math structure")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.vector2(1.0, 2.0); });
        CHECK(value_of(value).kind == value_kind::unsupported);
    }
    SECTION("rid is a fixed four-byte structure")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.rid(7); });
        CHECK(value_of(value).kind == value_kind::unsupported);
    }
    SECTION("legacy real-width math structure")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) { e.vector2(1.0, 2.0); });
        CHECK(read(build_resource(single_property(2, 1, "p", value))).records.front().properties.front().value.kind ==
              value_kind::unsupported);
    }
    SECTION("dictionary is traversed and represented unsupported")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.dictionary(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &k) { k.int32(1); }),
                encode_value(true, [](variant_encoder &v) { v.int32(3); }),
            });
        });
        CHECK(value_of(value).kind == value_kind::unsupported);
    }
    SECTION("nested dictionary inside an array")
    {
        const godot_resource_bytes dictionary = encode_value(true, [](variant_encoder &e) {
            e.dictionary(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &k) { k.int32(1); }),
                encode_value(true, [](variant_encoder &v) { v.rid(0); }),
            });
        });
        const godot_resource_bytes value = encode_value(
            true, [&dictionary](variant_encoder &e) { e.array(std::vector<godot_resource_bytes>{dictionary}); });
        const auto decoded = value_of(value);
        REQUIRE(decoded.kind == value_kind::array);
        REQUIRE(decoded.array.size() == 1);
        CHECK(decoded.array.front().kind == value_kind::unsupported);
    }
}

TEST_CASE("godot binary resource: represents string-keyed dictionaries")
{
    const auto value_at = [](const std::uint32_t major, const std::uint32_t format, const godot_resource_bytes &value) {
        return read(build_resource(single_property(major, format, "p", value)))
            .records.front()
            .properties.front()
            .value;
    };
    SECTION("godot 2 dictionary keeps names, values and order")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"alpha", encode_value(false, [](variant_encoder &v) { v.int32(7); })},
                {"beta", encode_value(false, [](variant_encoder &v) { v.boolean(true); })},
            });
        });
        const resource_value decoded = value_at(2, 1, value);
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 2);
        CHECK(decoded.dictionary[0].name == "alpha");
        CHECK(decoded.dictionary[0].value.kind == value_kind::integer);
        CHECK(decoded.dictionary[0].value.integer == 7);
        CHECK(decoded.dictionary[1].name == "beta");
        CHECK(decoded.dictionary[1].value.kind == value_kind::boolean);
        CHECK(decoded.dictionary[1].value.boolean);
    }
    SECTION("godot 3 dictionary")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"one", encode_value(false, [](variant_encoder &v) { v.int32(1); })},
            });
        });
        const resource_value decoded = value_at(3, 3, value);
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 1);
        CHECK(decoded.dictionary.front().name == "one");
        CHECK(decoded.dictionary.front().value.integer == 1);
    }
    SECTION("modern dictionary with the shared count bit set")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(
                std::vector<std::pair<std::string, godot_resource_bytes>>{
                    {"x", encode_value(true, [](variant_encoder &v) { v.int32(4); })},
                },
                true);
        });
        const resource_value decoded = value_at(4, 6, value);
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 1);
        CHECK(decoded.dictionary.front().value.integer == 4);
    }
    // A Godot 3 ``Image`` property is saved as the deprecated image dictionary, keyed by the
    // string names below; this is a genuine Godot 3 header, not a relabelled Godot 4 one.
    SECTION("godot 3 image data dictionary")
    {
        const std::vector<std::uint8_t> pixels{0x10, 0x20, 0x30};
        const godot_resource_bytes value = encode_value(false, [&pixels](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"width", encode_value(false, [](variant_encoder &v) { v.int32(2); })},
                {"height", encode_value(false, [](variant_encoder &v) { v.int32(1); })},
                {"format", encode_value(false, [](variant_encoder &v) { v.string("rgb8"); })},
                {"mipmaps", encode_value(false, [](variant_encoder &v) { v.boolean(false); })},
                {"data", encode_value(
                             false, [&pixels](variant_encoder &v) { v.bytes(std::span<const std::uint8_t>(pixels)); })},
            });
        });
        const resource_value decoded = value_at(3, 3, value);
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 5);
        CHECK(decoded.dictionary[0].name == "width");
        CHECK(decoded.dictionary[0].value.integer == 2);
        CHECK(decoded.dictionary[2].name == "format");
        CHECK(decoded.dictionary[2].value.string == "rgb8");
        REQUIRE(decoded.dictionary[4].value.kind == value_kind::bytes);
        CHECK(decoded.dictionary[4].value.bytes ==
              std::vector<std::byte>{std::byte{0x10}, std::byte{0x20}, std::byte{0x30}});
    }
    // A Godot 2 ``Sample`` property is saved as this string-keyed dictionary; the header is a
    // genuine Godot 2 one, not a relabelled Godot 3 document.
    SECTION("godot 2 sample data dictionary")
    {
        const std::vector<std::uint8_t> samples{1, 2, 3, 4};
        const godot_resource_bytes value = encode_value(false, [&samples](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"packing", encode_value(false, [](variant_encoder &v) { v.string("raw"); })},
                {"format", encode_value(false, [](variant_encoder &v) { v.string("pcm16"); })},
                {"stereo", encode_value(false, [](variant_encoder &v) { v.boolean(true); })},
                {"length", encode_value(false, [](variant_encoder &v) { v.int32(100); })},
                {"data",
                 encode_value(false,
                              [&samples](variant_encoder &v) { v.bytes(std::span<const std::uint8_t>(samples)); })},
            });
        });
        const resource_value decoded = value_at(2, 1, value);
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 5);
        CHECK(decoded.dictionary[0].name == "packing");
        CHECK(decoded.dictionary[0].value.string == "raw");
        CHECK(decoded.dictionary[4].name == "data");
        REQUIRE(decoded.dictionary[4].value.kind == value_kind::bytes);
        CHECK(decoded.dictionary[4].value.bytes.size() == 4);
    }
    SECTION("empty dictionary is represented")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{});
        });
        const resource_value decoded = value_at(4, 6, value);
        CHECK(decoded.kind == value_kind::dictionary);
        CHECK(decoded.dictionary.empty());
    }
    SECTION("nested dictionary through a dictionary and an array")
    {
        const godot_resource_bytes inner = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"leaf", encode_value(true, [](variant_encoder &v) { v.int32(9); })},
            });
        });
        const godot_resource_bytes middle = encode_value(true, [&inner](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"child", inner},
                {"empty", encode_value(true,
                                       [](variant_encoder &v) {
                                           v.string_dictionary(
                                               std::vector<std::pair<std::string, godot_resource_bytes>>{});
                                       })},
            });
        });
        const godot_resource_bytes value =
            encode_value(true, [&middle](variant_encoder &e) { e.array(std::vector<godot_resource_bytes>{middle}); });
        const resource_value decoded = value_at(4, 6, value);
        REQUIRE(decoded.kind == value_kind::array);
        REQUIRE(decoded.array.size() == 1);
        REQUIRE(decoded.array.front().kind == value_kind::dictionary);
        REQUIRE(decoded.array.front().dictionary.size() == 2);
        const resource_value &leaf = decoded.array.front().dictionary[0].value;
        REQUIRE(leaf.kind == value_kind::dictionary);
        REQUIRE(leaf.dictionary.size() == 1);
        CHECK(leaf.dictionary.front().name == "leaf");
        CHECK(leaf.dictionary.front().value.integer == 9);
        CHECK(decoded.array.front().dictionary[1].value.kind == value_kind::dictionary);
        CHECK(decoded.array.front().dictionary[1].value.dictionary.empty());
    }
}

TEST_CASE("godot binary resource: keeps non-string-keyed dictionaries unmodelled")
{
    const auto value_at = [](const std::uint32_t major, const std::uint32_t format, const godot_resource_bytes &value) {
        return read(build_resource(single_property(major, format, "p", value)))
            .records.front()
            .properties.front()
            .value;
    };

    SECTION("an int-keyed dictionary")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.dictionary(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &k) { k.int32(1); }),
                encode_value(true, [](variant_encoder &v) { v.int32(2); }),
            });
        });
        CHECK(value_at(4, 6, value).kind == value_kind::unsupported);
    }
    SECTION("a string key after a non-string key stays unmodelled")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.dictionary(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &k) { k.int32(1); }),
                encode_value(true, [](variant_encoder &v) { v.int32(2); }),
                encode_value(true, [](variant_encoder &k) { k.string("late"); }),
                encode_value(true, [](variant_encoder &v) { v.int32(3); }),
            });
        });
        CHECK(value_at(4, 6, value).kind == value_kind::unsupported);
    }
    SECTION("a represented dictionary with an unmodelled nested value")
    {
        const godot_resource_bytes nested = encode_value(true, [](variant_encoder &e) {
            e.dictionary(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &k) { k.int32(1); }),
                encode_value(true, [](variant_encoder &v) { v.int32(2); }),
            });
        });
        const godot_resource_bytes value = encode_value(true, [&nested](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"k", nested},
            });
        });
        const resource_value decoded = value_at(4, 6, value);
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 1);
        CHECK(decoded.dictionary.front().value.kind == value_kind::unsupported);
    }
}

TEST_CASE("godot binary resource: rejects malformed and duplicate dictionaries")
{
    const auto document = [](const godot_resource_bytes &value) {
        return build_resource(single_property(4, 6, "p", value));
    };
    const auto fits = [](const godot_resource_bytes &bytes, const std::uint64_t budget) {
        resource_limits limits;
        limits.max_allocation_bytes = budget;
        try {
            (void)read(bytes, true, limits);
            return true;
        } catch (const resource_error &) {
            return false;
        }
    };

    SECTION("a duplicate represented key rejects")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"dup", encode_value(true, [](variant_encoder &v) { v.int32(1); })},
                {"dup", encode_value(true, [](variant_encoder &v) { v.int32(2); })},
            });
        });
        CHECK_THROWS_AS(read(document(value)), resource_error);
    }
    SECTION("a duplicate separated by another key rejects")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"a", encode_value(true, [](variant_encoder &v) { v.int32(1); })},
                {"b", encode_value(true, [](variant_encoder &v) { v.int32(2); })},
                {"a", encode_value(true, [](variant_encoder &v) { v.int32(3); })},
            });
        });
        CHECK_THROWS_AS(read(document(value)), resource_error);
    }
    SECTION("a truncated dictionary pair rejects")
    {
        wire_writer out;
        out.u32(godot_tag_dictionary).u32(2);
        out.u32(godot_tag_string).unicode("a");
        out.u32(godot_tag_int).u32(1);
        CHECK_THROWS_AS(read(document(out.take())), resource_error);
    }
    SECTION("a malformed nested value rejects")
    {
        wire_writer out;
        out.u32(godot_tag_dictionary).u32(1);
        out.u32(godot_tag_string).unicode("a");
        out.u32(99);
        CHECK_THROWS_AS(read(document(out.take())), resource_error);
    }
    SECTION("the allocation ceiling charges the retained dictionary entries")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"a", encode_value(true, [](variant_encoder &v) { v.nil(); })},
                {"b", encode_value(true, [](variant_encoder &v) { v.nil(); })},
                {"c", encode_value(true, [](variant_encoder &v) { v.nil(); })},
                {"d", encode_value(true, [](variant_encoder &v) { v.nil(); })},
            });
        });
        const godot_resource_bytes bytes = document(value);
        REQUIRE(fits(bytes, 1U << 20U));
        CHECK(!(fits(bytes, 0)));
        std::uint64_t low = 0;
        std::uint64_t high = 1U << 20U;
        while (low < high) {
            const std::uint64_t middle = low + (high - low) / 2;
            if (fits(bytes, middle)) {
                high = middle;
            } else {
                low = middle + 1;
            }
        }
        REQUIRE(fits(bytes, low));
        CHECK(!(fits(bytes, low - 1)));
        // Four retained entries, each a property plus a decoded value, are charged on top of
        // the header, strings and transient pair, so the minimal budget cannot drop below the
        // retained entry slots.
        CHECK(low >= 4ULL * (sizeof(resource_property) + sizeof(resource_value)));
    }
    SECTION("the node ceiling bounds dictionary pairs")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"a", encode_value(true, [](variant_encoder &v) { v.int32(1); })},
                {"b", encode_value(true, [](variant_encoder &v) { v.int32(2); })},
                {"c", encode_value(true, [](variant_encoder &v) { v.int32(3); })},
            });
        });
        const godot_resource_bytes bytes = document(value);
        resource_limits limits;
        limits.max_nodes = 6;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        limits.max_nodes = 7;
        CHECK(read(bytes, true, limits).records.size() == 1);
    }
    // Regression: a three-pair dictionary needs six child nodes, but preflighting only
    // ``count`` nodes let the entry vector be charged and reserved first. The scaffolding
    // budget below (the smallest allocation that still parses an empty dictionary of the same
    // shape) makes the allocation ceiling a competing failure, so the rejection reason shows
    // the node preflight runs before the dictionary is allocated.
    SECTION("the node budget rejects a dictionary before its allocation is charged")
    {
        const auto minimal_allocation = [&fits](const godot_resource_bytes &bytes) {
            std::uint64_t low = 0;
            std::uint64_t high = 1;
            while (!fits(bytes, high)) {
                high *= 2;
            }
            while (low < high) {
                const std::uint64_t middle = low + (high - low) / 2;
                if (fits(bytes, middle)) {
                    high = middle;
                } else {
                    low = middle + 1;
                }
            }
            return low;
        };
        const godot_resource_bytes empty = document(encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{});
        }));
        const std::uint64_t scaffolding = minimal_allocation(empty);

        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"a", encode_value(true, [](variant_encoder &v) { v.nil(); })},
                {"b", encode_value(true, [](variant_encoder &v) { v.nil(); })},
                {"c", encode_value(true, [](variant_encoder &v) { v.nil(); })},
            });
        });
        resource_limits limits;
        limits.max_nodes = 4;
        limits.max_allocation_bytes = scaffolding;
        bool rejected = false;
        std::string message;
        try {
            (void)read(document(value), true, limits);
        } catch (const resource_error &error) {
            rejected = true;
            message = error.what();
        }
        REQUIRE(rejected);
        CHECK(message.find("node budget") != std::string::npos);
    }
    SECTION("the depth ceiling bounds nested dictionaries")
    {
        const godot_resource_bytes inner = encode_value(true, [](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"leaf", encode_value(true, [](variant_encoder &v) { v.int32(1); })},
            });
        });
        const godot_resource_bytes value = encode_value(true, [&inner](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"child", inner},
            });
        });
        const godot_resource_bytes bytes = document(value);
        resource_limits limits;
        limits.max_depth = 2;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        limits.max_depth = 3;
        CHECK(read(bytes, true, limits).records.size() == 1);
    }
}

TEST_CASE("godot binary resource: rejects tags that are unavailable for the generation")
{
    const auto build_tag = [](const std::uint32_t tag, const std::uint32_t major) {
        wire_writer out;
        out.u32(tag);
        return build_resource(single_property(major, major == 4 ? 6U : 3U, "p", out.take()));
    };
    SECTION("unknown tag")
    {
        CHECK_THROWS_AS(read(build_tag(99, 4)), resource_error);
    }
    SECTION("input event is not a Godot 3 tag")
    {
        CHECK_THROWS_AS(read(build_tag(godot_tag_input_event, 3)), resource_error);
    }
    SECTION("input event is not a modern tag")
    {
        CHECK_THROWS_AS(read(build_tag(godot_tag_input_event, 4)), resource_error);
    }
    SECTION("string name is not a Godot 3 tag")
    {
        CHECK_THROWS_AS(read(build_tag(godot_tag_string_name, 3)), resource_error);
    }
}

TEST_CASE("godot binary resource: rejects malformed strings and encodings")
{
    const auto string_value = [](const std::string &content) {
        return build_resource(single_property(4, 6, "p", raw_string(content)));
    };
    SECTION("invalid UTF-8 lead byte")
    {
        CHECK_THROWS_AS(read(string_value(std::string("\x80", 1))), resource_error);
    }
    SECTION("truncated UTF-8 sequence")
    {
        CHECK_THROWS_AS(read(string_value(std::string("\xC3", 1))), resource_error);
    }
    SECTION("invalid UTF-8 continuation byte")
    {
        CHECK_THROWS_AS(read(string_value(std::string("\xC3\x28", 2))), resource_error);
    }
    SECTION("overlong UTF-8 encoding")
    {
        CHECK_THROWS_AS(read(string_value(std::string("\xC0\x80", 2))), resource_error);
    }
    SECTION("surrogate UTF-8 code point")
    {
        CHECK_THROWS_AS(read(string_value(std::string("\xED\xA0\x80", 3))), resource_error);
    }
    SECTION("code point above the Unicode range")
    {
        CHECK_THROWS_AS(read(string_value(std::string("\xF5\x80\x80\x80", 4))), resource_error);
    }
    SECTION("missing NUL terminator")
    {
        wire_writer out;
        out.u32(godot_tag_string).u32(3).raw(std::vector<std::uint8_t>{'a', 'b', 'c'});
        CHECK_THROWS_AS(read(build_resource(single_property(4, 6, "p", out.take()))), resource_error);
    }
    SECTION("embedded NUL")
    {
        CHECK_THROWS_AS(read(string_value(std::string("a\0b", 3))), resource_error);
    }
    SECTION("valid multi-byte strings are accepted")
    {
        CHECK(read(string_value("A\xC3\xA9\xE2\x82\xAC\xF0\x9F\x98\x80")).records.size() == 1);
    }
    SECTION("empty string value")
    {
        wire_writer out;
        out.u32(godot_tag_string).u32(0);
        CHECK(read(build_resource(single_property(4, 6, "p", out.take())))
                  .records.front()
                  .properties.front()
                  .value.string.empty());
    }
}

TEST_CASE("godot binary resource: rejects malformed string tables and names")
{
    SECTION("legacy name index out of range")
    {
        resource_document_spec spec =
            single_property(3, 3, "p", encode_value(false, [](variant_encoder &e) { e.nil(); }));
        spec.records.front().properties.front().name_field = 4;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("modern name index out of range")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.nil(); }));
        spec.records.front().properties.front().name_field = 4;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("modern inline property name")
    {
        resource_document_spec spec = base_document(4, 6);
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        resource_property_spec property;
        property.name = "inline_name";
        property.value = encode_value(true, [](variant_encoder &e) { e.nil(); });
        record.properties.push_back(std::move(property));
        spec.records.push_back(std::move(record));
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.records.front().properties.front().name == "inline_name");
    }
    SECTION("empty property name")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.nil(); }));
        spec.records.front().properties.front().name_field = 0x80000000U;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("string table count exceeds the remaining bytes")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.nil(); }));
        spec.string_table_count = 100000;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
}

TEST_CASE("godot binary resource: enforces the input, allocation, node and depth budgets")
{
    const godot_resource_bytes minimal =
        build_resource(single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); })));

    SECTION("input budget")
    {
        resource_limits limits;
        limits.max_input_bytes = 8;
        CHECK_THROWS_AS(read(minimal, true, limits), resource_error);
    }
    SECTION("allocation budget")
    {
        resource_limits limits;
        limits.max_allocation_bytes = 0;
        CHECK_THROWS_AS(read(minimal, true, limits), resource_error);
    }
    SECTION("node budget")
    {
        resource_limits limits;
        limits.max_nodes = 0;
        CHECK_THROWS_AS(read(minimal, true, limits), resource_error);
    }
    SECTION("node budget boundary")
    {
        const godot_resource_bytes array =
            build_resource(single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) {
                                               e.array(std::vector<godot_resource_bytes>{
                                                   encode_value(true, [](variant_encoder &inner) { inner.int32(1); })});
                                           })));
        resource_limits limits;
        limits.max_nodes = 1;
        CHECK_THROWS_AS(read(array, true, limits), resource_error);
        limits.max_nodes = 2;
        CHECK(read(array, true, limits).records.size() == 1);
    }
    SECTION("depth budget")
    {
        const godot_resource_bytes array =
            build_resource(single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) {
                                               e.array(std::vector<godot_resource_bytes>{
                                                   encode_value(true, [](variant_encoder &inner) { inner.int32(1); })});
                                           })));
        resource_limits limits;
        limits.max_depth = 1;
        CHECK_THROWS_AS(read(array, true, limits), resource_error);
        limits.max_depth = 2;
        CHECK(read(array, true, limits).records.size() == 1);
    }
    SECTION("string budget")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.string("abcdefghij"); }));
        resource_limits limits;
        limits.max_string_bytes = 10;
        CHECK_THROWS_AS(read(build_resource(spec), true, limits), resource_error);
        limits.max_string_bytes = 11;
        CHECK(read(build_resource(spec), true, limits).records.size() == 1);
    }
}

TEST_CASE("godot binary resource: rejects truncated and out-of-range input")
{
    const godot_resource_bytes minimal =
        build_resource(single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); })));

    SECTION("truncated header")
    {
        godot_resource_bytes truncated = minimal;
        truncated.resize(6);
        CHECK_THROWS_AS(read(truncated), resource_error);
    }
    SECTION("truncated record")
    {
        godot_resource_bytes truncated = minimal;
        truncated.resize(minimal.size() - 3);
        CHECK_THROWS_AS(read(truncated), resource_error);
    }
    SECTION("internal resource offset out of range")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        spec.internal_offset = 0x1'0000'0000ULL;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("import metadata offset out of range")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        spec.import_offset = 0x1'0000'0000ULL;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("unsupported engine major version")
    {
        resource_document_spec spec =
            single_property(5, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("future format version")
    {
        resource_document_spec spec =
            single_property(4, 7, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("array count exceeds the remaining bytes")
    {
        wire_writer out;
        out.u32(godot_tag_array).u32(1000000);
        CHECK_THROWS_AS(read(build_resource(single_property(4, 6, "p", out.take()))), resource_error);
    }
    SECTION("property count exceeds the remaining bytes")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        spec.records.front().property_count = 100000;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
    SECTION("external resource count exceeds the remaining bytes")
    {
        resource_document_spec spec =
            single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); }));
        spec.external_count = 100000;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
}

TEST_CASE("godot binary resource: preserves multiple records with the root last")
{
    resource_document_spec spec = base_document(4, 6);
    spec.string_table.push_back("child");
    resource_record_spec first;
    first.path = "local://3";
    first.type = "First";
    resource_record_spec second;
    second.path = "res://second";
    second.type = "Second";
    resource_property_spec property;
    property.name = "child";
    property.value = encode_value(true, [](variant_encoder &e) { e.object_internal(3); });
    second.properties.push_back(std::move(property));
    spec.records.push_back(std::move(first));
    spec.records.push_back(std::move(second));
    const binary_resource resource = read(build_resource(spec));
    REQUIRE(resource.records.size() == 2);
    CHECK(resource.records.front().path == "local://3");
    CHECK(resource.records.back().path == "res://second");
    CHECK(resource.records.back().properties.front().value.reference.index == 0);
}

TEST_CASE("godot binary resource: reads the Godot 3 inline property name")
{
    SECTION("an inline name is accepted for Godot 3")
    {
        resource_document_spec spec = base_document(3, 3);
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        resource_property_spec property;
        // Not in the string table, so the fixture writes an inline (high-bit) name field.
        property.name = "inline3";
        property.value = encode_value(false, [](variant_encoder &e) { e.nil(); });
        record.properties.push_back(std::move(property));
        spec.records.push_back(std::move(record));
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(resource.records.size() == 1);
        CHECK(resource.records.front().properties.front().name == "inline3");
    }
    SECTION("a high-bit name field is rejected for Godot 2")
    {
        resource_document_spec spec =
            single_property(2, 1, "p", encode_value(false, [](variant_encoder &e) { e.nil(); }));
        spec.records.front().properties.front().name_field = 0x80000005U;
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
}

TEST_CASE("godot binary resource: structurally skips known unrelated variants")
{
    // Wraps ``value`` between two integer properties so a mis-sized skip is caught by the
    // trailing property rather than silently accepted.
    const auto around = [](const std::uint32_t major, const std::uint32_t format, const godot_resource_bytes &value) {
        resource_document_spec spec = base_document(major, format);
        spec.string_table.push_back("before");
        spec.string_table.push_back("middle");
        spec.string_table.push_back("after");
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        resource_property_spec before;
        before.name = "before";
        before.value = encode_value(major >= 4, [](variant_encoder &e) { e.int32(11); });
        resource_property_spec middle;
        middle.name = "middle";
        middle.value = value;
        resource_property_spec after;
        after.name = "after";
        after.value = encode_value(major >= 4, [](variant_encoder &e) { e.int32(22); });
        record.properties.push_back(std::move(before));
        record.properties.push_back(std::move(middle));
        record.properties.push_back(std::move(after));
        spec.records.push_back(std::move(record));
        return read(build_resource(spec));
    };
    const auto check_skipped = [](const binary_resource &resource) {
        REQUIRE(resource.records.size() == 1);
        REQUIRE(resource.records.front().properties.size() == 3);
        CHECK(resource.records.front().properties.at(0).value.integer == 11);
        CHECK(resource.records.front().properties.at(1).value.kind == value_kind::unsupported);
        CHECK(resource.records.front().properties.at(2).value.integer == 22);
    };

    SECTION("godot 3 node path")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) {
            e.node_path(std::vector<std::uint32_t>{0, 2}, std::vector<std::uint32_t>{1}, true, false);
        });
        check_skipped(around(3, 3, value));
    }
    SECTION("godot 2 node path with the trailing property field")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) {
            e.node_path(std::vector<std::uint32_t>{0}, std::vector<std::uint32_t>{1}, false, true);
        });
        check_skipped(around(2, 1, value));
    }
    SECTION("modern node path")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
            e.node_path(std::vector<std::uint32_t>{0}, std::vector<std::uint32_t>{}, false, false);
        });
        check_skipped(around(4, 6, value));
    }
    SECTION("modern string name")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.string_name("Node"); });
        check_skipped(around(4, 6, value));
    }
    SECTION("godot 2 input event")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) { e.input_event(); });
        check_skipped(around(2, 1, value));
    }
    SECTION("modern packed vector2 array")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.packed_vector2_array(2); });
        check_skipped(around(4, 6, value));
    }
    SECTION("modern packed colour array")
    {
        const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) { e.packed_color_array(2); });
        check_skipped(around(4, 6, value));
    }
    SECTION("legacy packed vector3 array")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) { e.packed_vector3_array(1); });
        check_skipped(around(3, 3, value));
    }
    SECTION("node path with an out-of-range name index")
    {
        const godot_resource_bytes value = encode_value(false, [](variant_encoder &e) {
            e.node_path(std::vector<std::uint32_t>{9}, std::vector<std::uint32_t>{}, false, false);
        });
        CHECK_THROWS_AS(around(3, 3, value), resource_error);
    }
    SECTION("truncated node path")
    {
        wire_writer out;
        out.u32(godot_tag_node_path).u16(4);
        CHECK_THROWS_AS(read(build_resource(single_property(3, 3, "p", out.take()))), resource_error);
    }
    SECTION("big-endian 16-bit node path counts")
    {
        resource_document_spec spec = base_document(3, 3);
        spec.big_endian = true;
        spec.string_table.push_back("name");
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        resource_property_spec property;
        property.name = "name";
        property.value = encode_value(
            false,
            [](variant_encoder &e) {
                e.node_path(std::vector<std::uint32_t>{0}, std::vector<std::uint32_t>{}, false, false);
            },
            false, true);
        record.properties.push_back(std::move(property));
        spec.records.push_back(std::move(record));
        const binary_resource resource = read(build_resource(spec));
        CHECK(resource.records.front().properties.front().value.kind == value_kind::unsupported);
    }
}

TEST_CASE("godot binary resource: validates the legacy subresource id index")
{
    const auto single = [](const std::string &path) {
        resource_document_spec spec = base_document(4, 6);
        resource_record_spec child;
        child.path = path;
        child.type = "Child";
        spec.records.push_back(std::move(child));
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        spec.records.push_back(std::move(root));
        return read(build_resource(spec));
    };
    SECTION("a nonlocal child path is not indexed as a legacy id")
    {
        const auto resource = single("res://child");
        REQUIRE(resource.records.size() == 2);
        CHECK(resource.records[0].path == "res://child");
    }
    SECTION("a bare negative sign is rejected")
    {
        CHECK_THROWS_AS(single("local://-"), resource_error);
    }
    SECTION("a negative id below signed 32-bit range is rejected")
    {
        CHECK_THROWS_AS(single("local://-2147483649"), resource_error);
    }
    SECTION("the lowest signed id retains its wire bits")
    {
        const auto resource = single("local://-2147483648");
        REQUIRE(resource.records.size() == 2);
        CHECK(resource.records[0].path == "local://-2147483648");
    }
    SECTION("an empty id is rejected")
    {
        CHECK_THROWS_AS(single("local://"), resource_error);
    }
    SECTION("a non-digit below zero is rejected")
    {
        CHECK_THROWS_AS(single("local:///1"), resource_error);
    }
    SECTION("a non-digit above nine is rejected")
    {
        CHECK_THROWS_AS(single("local://x1"), resource_error);
    }
    SECTION("an overflowing id is rejected")
    {
        CHECK_THROWS_AS(single("local://99999999999"), resource_error);
    }
    SECTION("two distinct ids are indexed")
    {
        resource_document_spec spec = base_document(4, 6);
        resource_record_spec first;
        first.path = "local://1";
        first.type = "Child";
        resource_record_spec second;
        second.path = "local://2";
        second.type = "Child";
        spec.records.push_back(std::move(first));
        spec.records.push_back(std::move(second));
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        spec.records.push_back(std::move(root));
        CHECK(read(build_resource(spec)).records.size() == 3);
    }
    SECTION("an id below the smallest indexed id is unknown")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back("p");
        resource_record_spec child;
        child.path = "local://5";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        resource_property_spec property;
        property.name = "p";
        property.value = encode_value(true, [](variant_encoder &e) { e.object_internal(3); });
        root.properties.push_back(std::move(property));
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        CHECK_THROWS_AS(read(build_resource(spec)), resource_error);
    }
}

TEST_CASE("godot binary resource: a legacy main record may carry the local negative-one path",
          "[godot][binary-resource][main-local-id]")
{
    resource_document_spec spec = base_document(3, 3);
    resource_record_spec main;
    main.path = "local://-1";
    main.type = "Sample";
    spec.main_type = "Sample";
    spec.records.push_back(std::move(main));
    const auto stored = build_resource(spec);
    CHECK(read(stored).records.back().path == "local://-1");
    const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        for (std::size_t index = 0; index < target.size(); ++index)
            target[index] = static_cast<std::byte>(stored[static_cast<std::size_t>(offset) + index]);
    };
    CHECK(unpackrat::godot::read_binary_resource_metadata(stored.size(), source, true).records.back().path ==
          "local://-1");
    resource_limits bounded;
    bounded.max_string_bytes = 2;
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata(stored.size(), source, true, bounded),
                    resource_error);
}

TEST_CASE("godot binary resource: reads a legacy external resource table")
{
    resource_document_spec spec = base_document(3, 3);
    spec.external_resources.push_back({"Texture", "res://ext.png"});
    spec.string_table.push_back("p");
    resource_record_spec root;
    root.path = "res://root";
    root.type = "Root";
    resource_property_spec property;
    property.name = "p";
    property.value = encode_value(false, [](variant_encoder &e) { e.object_external_index(0); });
    root.properties.push_back(std::move(property));
    spec.records.push_back(std::move(root));
    const binary_resource resource = read(build_resource(spec));
    REQUIRE(resource.external_resources.size() == 1);
    CHECK(resource.external_resources.front().type == "Texture");
}

TEST_CASE("godot binary resource: bounds dictionary children by the node budget")
{
    const godot_resource_bytes value = encode_value(true, [](variant_encoder &e) {
        e.dictionary(std::vector<godot_resource_bytes>{
            encode_value(true, [](variant_encoder &key) { key.int32(1); }),
            encode_value(true, [](variant_encoder &entry) { entry.int32(2); }),
        });
    });
    const godot_resource_bytes bytes = build_resource(single_property(4, 6, "p", value));
    resource_limits limits;
    limits.max_nodes = 2;
    CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
    limits.max_nodes = 3;
    CHECK(read(bytes, true, limits).records.size() == 1);
}

// A parent container preflights only its own direct children, so a child container that is
// itself a pair value can add nodes after that preflight has already passed. The cumulative
// total is then caught by the per-value node guard, not the preflight. ``{"nested": [nil],
// "after": nil}`` is one dictionary, two string keys, a nested array, its nil element and a
// trailing nil value: six nodes. A five-node budget rejects at the guard, and six nodes
// retains every represented value.
TEST_CASE("godot binary resource: the per-value node guard bounds cumulative nested nodes")
{
    SECTION("dictionary with a nested array element")
    {
        const godot_resource_bytes nested = encode_value(true, [](variant_encoder &e) {
            e.array(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &v) { v.nil(); }),
            });
        });
        const godot_resource_bytes value = encode_value(true, [&nested](variant_encoder &e) {
            e.string_dictionary(std::vector<std::pair<std::string, godot_resource_bytes>>{
                {"nested", nested},
                {"after", encode_value(true, [](variant_encoder &v) { v.nil(); })},
            });
        });
        const godot_resource_bytes bytes = build_resource(single_property(4, 6, "p", value));
        resource_limits limits;
        limits.max_nodes = 5;
        bool rejected = false;
        std::string message;
        try {
            (void)read(bytes, true, limits);
        } catch (const resource_error &error) {
            rejected = true;
            message = error.what();
        }
        REQUIRE(rejected);
        CHECK(message == "resource exceeds the value node budget");
        limits.max_nodes = 6;
        const resource_value decoded = read(bytes, true, limits).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::dictionary);
        REQUIRE(decoded.dictionary.size() == 2);
        CHECK(decoded.dictionary[0].name == "nested");
        REQUIRE(decoded.dictionary[0].value.kind == value_kind::array);
        REQUIRE(decoded.dictionary[0].value.array.size() == 1);
        CHECK(decoded.dictionary[0].value.array.front().kind == value_kind::nil);
        CHECK(decoded.dictionary[1].name == "after");
        CHECK(decoded.dictionary[1].value.kind == value_kind::nil);
    }
    SECTION("array with a nested array element")
    {
        const godot_resource_bytes inner = encode_value(true, [](variant_encoder &e) {
            e.array(std::vector<godot_resource_bytes>{
                encode_value(true, [](variant_encoder &v) { v.nil(); }),
            });
        });
        const godot_resource_bytes value = encode_value(true, [&inner](variant_encoder &e) {
            e.array(std::vector<godot_resource_bytes>{
                inner,
                encode_value(true, [](variant_encoder &v) { v.nil(); }),
            });
        });
        const godot_resource_bytes bytes = build_resource(single_property(4, 6, "p", value));
        resource_limits limits;
        limits.max_nodes = 3;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        limits.max_nodes = 4;
        const resource_value decoded = read(bytes, true, limits).records.front().properties.front().value;
        REQUIRE(decoded.kind == value_kind::array);
        REQUIRE(decoded.array.size() == 2);
        REQUIRE(decoded.array[0].kind == value_kind::array);
        REQUIRE(decoded.array[0].array.size() == 1);
        CHECK(decoded.array[0].array.front().kind == value_kind::nil);
        CHECK(decoded.array[1].kind == value_kind::nil);
    }
}

TEST_CASE("godot binary resource: uses the wide real for 64-bit real arrays")
{
    SECTION("legacy 64-bit real array")
    {
        const godot_resource_bytes value =
            encode_value(false, [](variant_encoder &e) { e.real_array(std::vector<double>{1.5, 2.5}); }, true);
        resource_document_spec spec = single_property(3, 3, "p", value);
        spec.use_real64 = true;
        const auto decoded = read(build_resource(spec)).records.front().properties.front().value;
        CHECK(decoded.real_array == std::vector<double>{1.5, 2.5});
    }
    SECTION("legacy 64-bit packed vector array keeps the wide element")
    {
        const godot_resource_bytes value =
            encode_value(false, [](variant_encoder &e) { e.packed_vector2_array(2); }, true);
        resource_document_spec spec = single_property(3, 3, "p", value);
        spec.use_real64 = true;
        CHECK(read(build_resource(spec)).records.front().properties.front().value.kind == value_kind::unsupported);
    }
}

TEST_CASE("godot binary resource: reads the external resource table without UIDs")
{
    resource_document_spec spec = base_document(4, 6);
    spec.external_resources.push_back({"Texture", "res://ext.png"});
    spec.string_table.push_back("p");
    resource_record_spec root;
    root.path = "res://root";
    root.type = "Root";
    resource_property_spec property;
    property.name = "p";
    property.value = encode_value(true, [](variant_encoder &e) { e.nil(); });
    root.properties.push_back(std::move(property));
    spec.records.push_back(std::move(root));
    const binary_resource resource = read(build_resource(spec));
    REQUIRE(resource.external_resources.size() == 1);
    CHECK(resource.external_resources.front().path == "res://ext.png");
    CHECK(resource.external_resources.front().type == "Texture");
}

TEST_CASE("godot binary resource: validates the four-byte magic")
{
    const godot_resource_bytes valid =
        build_resource(single_property(4, 6, "p", encode_value(true, [](variant_encoder &e) { e.int32(1); })));
    const auto with_magic = [&valid](const char *magic) {
        godot_resource_bytes bytes = valid;
        for (std::size_t index = 0; index < 4; ++index) {
            bytes[index] = static_cast<std::uint8_t>(magic[index]);
        }
        return bytes;
    };
    SECTION("the RSRC magic is accepted")
    {
        CHECK(read(valid).records.size() == 1);
    }
    SECTION("the RSCC magic needs the compressed wrapper")
    {
        CHECK_THROWS_AS(read(with_magic("RSCC")), resource_error);
    }
    SECTION("a wrong first byte is rejected")
    {
        CHECK_THROWS_AS(read(with_magic("XSRC")), resource_error);
    }
    SECTION("a wrong second byte is rejected")
    {
        CHECK_THROWS_AS(read(with_magic("RXRC")), resource_error);
    }
    SECTION("a wrong third byte is rejected")
    {
        CHECK_THROWS_AS(read(with_magic("RSRX")), resource_error);
    }
    SECTION("a wrong fourth byte is rejected")
    {
        CHECK_THROWS_AS(read(with_magic("RSCX")), resource_error);
    }
    SECTION("a short input is rejected")
    {
        CHECK_THROWS_AS(read(godot_resource_bytes{0x52, 0x53}, true), resource_error);
    }
}

TEST_CASE("godot binary resource: charges every owned string copy against the allocation budget")
{
    SECTION("repeated table-backed property names")
    {
        const std::string long_name(1000, 'n');
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back(long_name);
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        for (int index = 0; index < 6; ++index) {
            resource_property_spec property;
            property.name = long_name;
            property.value = encode_value(true, [](variant_encoder &e) { e.nil(); });
            record.properties.push_back(std::move(property));
        }
        spec.records.push_back(std::move(record));
        const godot_resource_bytes bytes = build_resource(spec);
        resource_limits limits;
        // A budget that one table string comfortably fits, but six copies do not.
        limits.max_allocation_bytes = 4000;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        CHECK(read(bytes).records.front().properties.size() == 6);
    }
    SECTION("record path copies")
    {
        const std::string long_path = "res://" + std::string(4000, 'p');
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back("p");
        resource_record_spec record;
        record.path = long_path;
        record.type = "Resource";
        resource_property_spec property;
        property.name = "p";
        property.value = encode_value(true, [](variant_encoder &e) { e.nil(); });
        record.properties.push_back(std::move(property));
        spec.records.push_back(std::move(record));
        const godot_resource_bytes bytes = build_resource(spec);
        resource_limits limits;
        // One read of the path fits, but the additional owned copy does not.
        limits.max_allocation_bytes = 6000;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        CHECK(read(bytes).records.front().path == long_path);
    }
}

TEST_CASE("godot binary resource: charges the legacy subresource id index against the allocation budget")
{
    // Two records with no properties, so the named and legacy documents carry identical wire
    // data apart from the named-scene-ids flag: "local://1" is a valid opaque named id as well
    // as a valid numeric legacy id. Only the legacy mode builds the subresource id index.
    const auto document = [](const bool named) {
        resource_document_spec spec = base_document(4, 6);
        spec.named_scene_ids = named;
        resource_record_spec child;
        child.path = "local://1";
        child.type = "Child";
        resource_record_spec root;
        root.path = "res://root";
        root.type = "Root";
        spec.records.push_back(std::move(child));
        spec.records.push_back(std::move(root));
        return build_resource(spec);
    };
    const godot_resource_bytes named_bytes = document(true);
    const godot_resource_bytes legacy_bytes = document(false);

    // Succeeds only if the document fits the allocation budget; every other failure propagates.
    const auto fits = [](const godot_resource_bytes &bytes, const std::uint64_t budget) {
        resource_limits limits;
        limits.max_allocation_bytes = budget;
        try {
            (void)read(bytes, true, limits);
            return true;
        } catch (const resource_error &) {
            return false;
        }
    };

    // The generous ceiling must accept the named document before narrowing the search.
    REQUIRE(fits(named_bytes, 4096));

    // Minimal successful allocation budget for the named document, which needs no id index.
    std::uint64_t low = 0;
    std::uint64_t high = 4096;
    while (low < high) {
        const std::uint64_t middle = low + (high - low) / 2;
        if (fits(named_bytes, middle)) {
            high = middle;
        } else {
            low = middle + 1;
        }
    }
    REQUIRE(fits(named_bytes, low));

    // The legacy document performs the same allocations plus its subresource id index, so at the
    // named document's exact minimal budget the uncharged index must overflow the budget.
    resource_limits tight;
    tight.max_allocation_bytes = low;
    CHECK_THROWS_AS(read(legacy_bytes, true, tight), resource_error);

    // The legacy document is still valid input: with a generous budget it decodes in full, so the
    // tight-budget failure above is the index cost and not an unrelated parse failure.
    resource_limits generous;
    generous.max_allocation_bytes = 4096;
    const binary_resource legacy = read(legacy_bytes, true, generous);
    REQUIRE(legacy.records.size() == 2);
    CHECK(legacy.records[0].path == "local://1");
    CHECK(legacy.records[0].type == "Child");
    CHECK(legacy.records[1].path == "res://root");
    CHECK(legacy.records[1].type == "Root");
}

TEST_CASE("godot binary resource: preflights child counts against the node budget")
{
    SECTION("array children")
    {
        wire_writer out;
        out.u32(godot_tag_array).u32(4);
        for (std::uint32_t index = 0; index < 4; ++index) {
            out.u32(test::support::godot_tag_int).u32(index);
        }
        const godot_resource_bytes bytes = build_resource(single_property(4, 6, "p", out.take()));
        resource_limits limits;
        limits.max_nodes = 3;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        limits.max_nodes = 5;
        CHECK(read(bytes, true, limits).records.size() == 1);
    }
    SECTION("packed string array children")
    {
        wire_writer out;
        out.u32(test::support::godot_tag_string_array).u32(4);
        for (int index = 0; index < 4; ++index) {
            out.unicode("s");
        }
        const godot_resource_bytes bytes = build_resource(single_property(4, 6, "p", out.take()));
        resource_limits limits;
        limits.max_nodes = 3;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
    }
    // A packed string array element is a single child node, not a key/value pair. Three
    // strings plus the container property fit exactly in a four-node budget, and a budget of
    // three must reject up front.
    SECTION("packed string array node budget boundary")
    {
        wire_writer out;
        out.u32(test::support::godot_tag_string_array).u32(3);
        for (int index = 0; index < 3; ++index) {
            out.unicode("s");
        }
        const godot_resource_bytes bytes = build_resource(single_property(4, 6, "p", out.take()));
        resource_limits limits;
        limits.max_nodes = 4;
        CHECK(read(bytes, true, limits).records.size() == 1);
        limits.max_nodes = 3;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
    }
    SECTION("record property preflight")
    {
        resource_document_spec spec = base_document(4, 6);
        spec.string_table.push_back("p");
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        for (int index = 0; index < 4; ++index) {
            resource_property_spec property;
            property.name = "p";
            property.value = encode_value(true, [](variant_encoder &e) { e.nil(); });
            record.properties.push_back(std::move(property));
        }
        spec.records.push_back(std::move(record));
        const godot_resource_bytes bytes = build_resource(spec);
        resource_limits limits;
        limits.max_nodes = 3;
        CHECK_THROWS_AS(read(bytes, true, limits), resource_error);
        limits.max_nodes = 4;
        CHECK(read(bytes, true, limits).records.size() == 1);
    }
}

TEST_CASE("godot binary resource: metadata prefix fetch respects the bounded scratch buffer",
          "[godot][binary-resource][metadata-bound]")
{
    const godot_resource_bytes value =
        encode_value(true, [](variant_encoder &encoder) { encoder.bytes(std::vector<std::uint8_t>(80, 0x5A)); });
    const godot_resource_bytes stored = build_resource(single_property(4, 6, "p", value));
    const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        for (std::size_t index = 0; index < target.size(); ++index)
            target[index] = static_cast<std::byte>(stored[static_cast<std::size_t>(offset) + index]);
    };
    resource_limits tight;
    tight.max_string_bytes = 32;
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata(stored.size(), source, true, tight),
                    resource_error);
    tight.max_string_bytes = 64;
    const auto decoded = unpackrat::godot::read_binary_resource_metadata(stored.size(), source, true, tight);
    REQUIRE(decoded.records.front().properties.front().value.bytes.size() == 64);
    CHECK(decoded.records.front().properties.front().value.byte_length == 80);
}

TEST_CASE("godot binary resource: metadata scalars use exact-width reads and skip media bodies",
          "[godot][binary-resource][metadata-width]")
{
    for (const bool big_endian : {false, true}) {
        CAPTURE(big_endian);
        resource_document_spec spec = base_document(4, 6);
        spec.big_endian = big_endian;
        spec.string_table = {"data", "path"};
        resource_record_spec record;
        record.path = "res://root";
        record.type = "Resource";
        resource_property_spec data;
        data.name = "data";
        data.value = encode_value(
            true, [](variant_encoder &encoder) { encoder.bytes(std::vector<std::uint8_t>(96, 0x5A)); }, false,
            big_endian);
        record.properties.push_back(std::move(data));
        resource_property_spec path;
        path.name = "path";
        path.value = encode_value(
            true,
            [](variant_encoder &encoder) {
                encoder.node_path(std::span<const std::uint32_t>{}, std::span<const std::uint32_t>{}, false, false);
            },
            false, big_endian);
        record.properties.push_back(std::move(path));
        spec.records.push_back(std::move(record));
        const godot_resource_bytes stored = build_resource(spec);

        std::array<std::size_t, 9> widths{};
        std::vector<std::pair<std::uint64_t, std::size_t>> requests;
        const auto source = [&stored, &widths, &requests](const std::uint64_t offset,
                                                          const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            requests.emplace_back(offset, target.size());
            if (target.size() < widths.size())
                ++widths[target.size()];
            for (std::size_t index = 0; index < target.size(); ++index)
                target[index] = static_cast<std::byte>(stored[static_cast<std::size_t>(offset) + index]);
        };
        const binary_resource decoded = unpackrat::godot::read_binary_resource_metadata(stored.size(), source, true);
        REQUIRE(decoded.records.size() == 1);
        REQUIRE(decoded.records.front().properties.size() == 2);
        const resource_value &media = decoded.records.front().properties.front().value;
        CHECK(media.kind == value_kind::bytes);
        CHECK(media.byte_length == 96);
        CHECK(media.bytes.size() == 64);
        CHECK(decoded.records.front().properties.back().value.kind == value_kind::unsupported);
        CHECK(widths[1] == 0);
        CHECK(widths[2] >= 2);
        CHECK(widths[4] > 0);
        CHECK(widths[8] > 0);
        const std::uint64_t skipped_begin = media.byte_offset + 64;
        const std::uint64_t skipped_end = media.byte_offset + media.byte_length;
        for (const auto &[offset, count] : requests)
            CHECK((offset + count <= skipped_begin || offset >= skipped_end));
    }
}

TEST_CASE("godot binary resource: rejected main types stop after a validated header",
          "[godot][binary-resource][header-admission]")
{
    struct late_read final : std::exception
    {
    };
    for (const std::uint32_t major : {3U, 4U}) {
        for (const bool big_endian : {false, true}) {
            for (const bool has_magic : {false, true}) {
                CAPTURE(major, big_endian, has_magic);
                resource_document_spec spec = single_property(
                    major, major == 3 ? 3U : 6U, "data",
                    encode_value(
                        major == 4,
                        [](variant_encoder &encoder) { encoder.bytes(std::vector<std::uint8_t>(96, 0x5A)); }, false,
                        big_endian));
                spec.main_type = "UnrelatedResource";
                spec.big_endian = big_endian;
                spec.include_magic = has_magic;
                const godot_resource_bytes stored = build_resource(spec);
                // Both fixture layouts have 56 bytes after the import offset when no script
                // class is present. The first table count begins exactly at this boundary.
                const std::size_t header_end =
                    (has_magic ? std::size_t{4} : 0) + 20 + 4 + spec.main_type.size() + 1 + 8 + 56;
                REQUIRE(header_end < stored.size());
                const auto source = [&](const std::uint64_t offset, const std::span<std::byte> target) {
                    if (offset >= header_end || target.size() > header_end - offset)
                        throw late_read{};
                    for (std::size_t index = 0; index < target.size(); ++index)
                        target[index] = static_cast<std::byte>(stored[static_cast<std::size_t>(offset) + index]);
                };
                std::size_t predicate_calls = 0;
                std::string observed_type;
                const auto reject = [&](const std::string_view type) {
                    ++predicate_calls;
                    observed_type = type;
                    return false;
                };
                bool touched_table = false;
                std::optional<binary_resource> admitted;
                try {
                    admitted =
                        unpackrat::godot::read_binary_resource_metadata_if(stored.size(), source, reject, has_magic);
                } catch (const late_read &) {
                    touched_table = true;
                }
                CHECK(!touched_table);
                CHECK(!admitted.has_value());
                CHECK(predicate_calls == 1);
                CHECK(observed_type == spec.main_type);
            }
        }
    }
}

TEST_CASE("godot binary resource: admitted metadata matches one strict parse without media-body reads",
          "[godot][binary-resource][header-admission]")
{
    for (const bool has_magic : {false, true}) {
        CAPTURE(has_magic);
        resource_document_spec spec = single_property(
            4, 6, "data",
            encode_value(true, [](variant_encoder &encoder) { encoder.bytes(std::vector<std::uint8_t>(96, 0x5A)); }),
            "AudioStreamWAV");
        spec.main_type = "AudioStreamWAV";
        spec.include_magic = has_magic;
        const godot_resource_bytes stored = build_resource(spec);
        using request = std::pair<std::uint64_t, std::size_t>;
        const auto source_for = [&stored](std::vector<request> &requests) {
            return [&stored, &requests](const std::uint64_t offset, const std::span<std::byte> target) {
                REQUIRE(offset <= stored.size());
                REQUIRE(target.size() <= stored.size() - offset);
                requests.emplace_back(offset, target.size());
                for (std::size_t index = 0; index < target.size(); ++index)
                    target[index] = static_cast<std::byte>(stored[static_cast<std::size_t>(offset) + index]);
            };
        };
        std::vector<request> strict_requests;
        const binary_resource strict =
            unpackrat::godot::read_binary_resource_metadata(stored.size(), source_for(strict_requests), has_magic);
        std::vector<request> admitted_requests;
        std::size_t predicate_calls = 0;
        const auto admitted = unpackrat::godot::read_binary_resource_metadata_if(
            stored.size(), source_for(admitted_requests),
            [&](const std::string_view type) {
                ++predicate_calls;
                return type == "AudioStreamWAV";
            },
            has_magic);
        REQUIRE(admitted.has_value());
        if (!admitted.has_value())
            return;
        CHECK(predicate_calls == 1);
        CHECK(admitted_requests == strict_requests);
        CHECK(admitted->main_type == strict.main_type);
        REQUIRE(admitted->records.size() == strict.records.size());
        REQUIRE(admitted->records.front().properties.size() == strict.records.front().properties.size());
        const resource_value &media = admitted->records.front().properties.front().value;
        const resource_value &expected = strict.records.front().properties.front().value;
        CHECK(media.kind == value_kind::bytes);
        CHECK(media.bytes == expected.bytes);
        CHECK(media.byte_offset == expected.byte_offset);
        CHECK(media.byte_length == 96);
        CHECK(media.bytes.size() == 64);
        for (const auto &[offset, count] : admitted_requests)
            CHECK((offset + count <= media.byte_offset + 64 || offset >= media.byte_offset + media.byte_length));
    }
}

TEST_CASE("godot binary resource: header admission preserves bounds and callback failures",
          "[godot][binary-resource][header-admission]")
{
    struct source_failure final : std::exception
    {
    };
    struct predicate_failure final : std::exception
    {
    };
    const godot_resource_bytes valid = build_resource(
        single_property(4, 6, "p", encode_value(true, [](variant_encoder &encoder) { encoder.int32(7); })));
    const auto source = [&valid](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= valid.size());
        REQUIRE(target.size() <= valid.size() - offset);
        for (std::size_t index = 0; index < target.size(); ++index)
            target[index] = static_cast<std::byte>(valid[static_cast<std::size_t>(offset) + index]);
    };
    std::size_t predicate_calls = 0;
    const auto reject = [&](std::string_view) {
        ++predicate_calls;
        return false;
    };
    resource_limits too_small;
    too_small.max_input_bytes = valid.size() - 1;
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata_if(valid.size(), source, reject, true, too_small),
                    resource_error);
    CHECK(predicate_calls == 0);

    resource_document_spec invalid_spec = base_document(5, 6);
    const godot_resource_bytes invalid_header = build_resource(invalid_spec);
    const auto invalid_source = [&invalid_header](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= invalid_header.size());
        REQUIRE(target.size() <= invalid_header.size() - offset);
        for (std::size_t index = 0; index < target.size(); ++index)
            target[index] = static_cast<std::byte>(invalid_header[static_cast<std::size_t>(offset) + index]);
    };
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata_if(invalid_header.size(), invalid_source, reject),
                    resource_error);
    CHECK(predicate_calls == 0);
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata_if(12, source, reject), resource_error);
    CHECK(predicate_calls == 0);

    const auto broken_source = [](std::uint64_t, std::span<std::byte>) { throw source_failure{}; };
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata_if(valid.size(), broken_source, reject),
                    source_failure);
    CHECK(predicate_calls == 0);

    const auto exploding = [](std::string_view) -> bool { throw predicate_failure{}; };
    CHECK_THROWS_AS(unpackrat::godot::read_binary_resource_metadata_if(valid.size(), source, exploding),
                    predicate_failure);
}
