#include "../../core/godot/font_resource.h"

#include <catch2/catch_test_macros.hpp>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace
{
    using unpackrat::godot::binary_resource;
    using unpackrat::godot::font_error;
    using unpackrat::godot::font_resource_extension;
    using unpackrat::godot::resource_property;
    using unpackrat::godot::resource_record;
    using unpackrat::godot::resource_value;
    using unpackrat::godot::value_kind;
    using unpackrat::godot::write_font_resource;

    binary_resource font(std::string_view signature, std::string_view field = "data/0")
    {
        binary_resource resource;
        resource.main_type = "FontFile";
        resource.engine.major = 4;
        resource.records.emplace_back();
        resource.records.back().type = "FontFile";
        resource_value data;
        data.kind = value_kind::bytes;
        for (const char value : signature) {
            data.bytes.push_back(static_cast<std::byte>(value));
        }
        resource.records.back().properties.push_back(resource_property{std::string(field), std::move(data)});
        return resource;
    }

    std::vector<std::byte> extract(const binary_resource &resource)
    {
        std::vector<std::byte> output;
        write_font_resource(resource, [&output](const std::span<const std::byte> bytes) {
            output.insert(output.end(), bytes.begin(), bytes.end());
        });
        return output;
    }
} // namespace

TEST_CASE("godot font: FontFile bytes are extracted unchanged using the embedded signature", "[godot][font]")
{
    const std::vector<std::pair<std::string_view, std::string_view>> cases = {
        {std::string_view("\x00\x01\x00\x00"
                          "payload",
                          11),
         "ttf"},
        {"truepayload", "ttf"},
        {"OTTOpayload", "otf"},
        {"ttcfpayload", "ttc"},
        {"wOFFpayload", "woff"},
        {"wOF2payload", "woff2"},
    };
    for (const auto &[signature, extension] : cases) {
        CAPTURE(extension);
        const binary_resource resource = font(signature);
        REQUIRE(font_resource_extension(resource) == std::optional<std::string_view>(extension));
        const auto output = extract(resource);
        REQUIRE(output == resource.records.back().properties.front().value.bytes);
    }
}

TEST_CASE("godot font: imported FontFile uses the data property", "[godot][font][imported]")
{
    const binary_resource resource = font("OTTOpayload", "data");
    REQUIRE(font_resource_extension(resource) == std::optional<std::string_view>("otf"));
    REQUIRE(extract(resource) == resource.records.back().properties.front().value.bytes);
}

TEST_CASE("godot font: metadata exposes the validated embedded byte range", "[godot][font][font-range]")
{
    binary_resource resource = font("OTTO", "data/0");
    resource_value &value = resource.records.back().properties.front().value;
    value.byte_offset = 127;
    value.byte_length = 4096;

    const auto info = unpackrat::godot::inspect_font_resource(resource);
    REQUIRE(info.has_value());
    if (!info.has_value()) {
        return;
    }
    CHECK(info.value().extension == "otf");
    CHECK(info.value().byte_offset == 127);
    CHECK(info.value().byte_length == 4096);
    CHECK(font_resource_extension(resource) == std::optional<std::string_view>("otf"));

    const binary_resource full = font("wOF2payload", "data");
    const auto full_info = unpackrat::godot::inspect_font_resource(full);
    REQUIRE(full_info.has_value());
    if (!full_info.has_value()) {
        return;
    }
    CHECK(full_info.value().extension == "woff2");
    CHECK(full_info.value().byte_length == full.records.back().properties.front().value.bytes.size());

    CHECK(!unpackrat::godot::inspect_font_resource(font("NOPEpayload")).has_value());

    binary_resource inconsistent = font("OTTOpayload");
    inconsistent.records.back().properties.front().value.byte_length = 2;
    CHECK_THROWS_AS(unpackrat::godot::inspect_font_resource(inconsistent), font_error);
    CHECK_THROWS_AS(font_resource_extension(inconsistent), font_error);
}

TEST_CASE("godot font: unrelated properties do not hide the embedded font", "[godot][font]")
{
    binary_resource resource = font("OTTOpayload", "data/0");
    resource.records.back().properties.insert(resource.records.back().properties.begin(),
                                              resource_property{"fallbacks", resource_value{}});
    REQUIRE(font_resource_extension(resource) == std::optional<std::string_view>("otf"));
    REQUIRE(extract(resource) == resource.records.back().properties.back().value.bytes);
}

TEST_CASE("godot font: only a root FontFile with one byte payload is converted", "[godot][font]")
{
    SECTION("unknown capture remains raw")
    {
        REQUIRE(!font_resource_extension(font("NOPEpayload")).has_value());
        REQUIRE_THROWS_AS(extract(font("NOPEpayload")), font_error);
    }
    SECTION("short capture remains raw")
    {
        REQUIRE(!font_resource_extension(font("OT")).has_value());
    }
    SECTION("missing payload remains raw")
    {
        binary_resource resource = font("OTTOpayload");
        resource.records.back().properties.clear();
        REQUIRE(!font_resource_extension(resource).has_value());
    }
    SECTION("wrong class remains raw")
    {
        binary_resource resource = font("OTTOpayload");
        resource.main_type = "Other";
        resource.records.back().type = "Other";
        REQUIRE(!font_resource_extension(resource).has_value());
    }
    SECTION("a missing root remains raw")
    {
        binary_resource resource = font("OTTOpayload");
        resource.records.clear();
        REQUIRE(!font_resource_extension(resource).has_value());
    }
    SECTION("root class mismatch is malformed")
    {
        binary_resource resource = font("OTTOpayload");
        resource.main_type = "Other";
        REQUIRE_THROWS_AS(font_resource_extension(resource), font_error);
    }
    SECTION("duplicate font bytes are malformed")
    {
        binary_resource resource = font("OTTOpayload");
        resource.records.back().properties.push_back(resource.records.back().properties.front());
        REQUIRE_THROWS_AS(font_resource_extension(resource), font_error);
    }
    SECTION("both font data spellings are ambiguous")
    {
        binary_resource resource = font("OTTOpayload", "data");
        resource.records.back().properties.push_back(
            resource_property{"data/0", resource.records.back().properties.front().value});
        REQUIRE_THROWS_AS(font_resource_extension(resource), font_error);
    }
    SECTION("wrong payload variant is malformed")
    {
        binary_resource resource = font("OTTOpayload");
        resource.records.back().properties.front().value.kind = value_kind::integer;
        REQUIRE_THROWS_AS(font_resource_extension(resource), font_error);
    }
    SECTION("sink failure propagates")
    {
        const binary_resource resource = font("OTTOpayload");
        REQUIRE_THROWS_AS(
            write_font_resource(resource, [](std::span<const std::byte>) { throw std::runtime_error("sink fault"); }),
            std::runtime_error);
    }
}
TEST_CASE("godot font: class admission recognizes only FontFile", "[godot][font][class-admission]")
{
    const std::pair<std::string_view, bool> cases[] = {
        {"FontFile", true},           {"Font", false}, {"fontfile", false}, {"FontFile2", false},
        {"OggPacketSequence", false}, {"", false},
    };
    for (const auto &[class_name, expected] : cases) {
        CAPTURE(class_name);
        CHECK(unpackrat::godot::supports_font_resource_class(class_name) == expected);
    }
}
