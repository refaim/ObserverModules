#include "../../core/godot/pck.h"
#include "../support/godot_fixtures.h"

#include "../../core/archive_limits.h"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using test::support::godot_bytes;
    using test::support::godot_entry_options;
    using test::support::godot_pck_options;
    using test::support::godot_pe_section;

    using unpackrat::godot::byte_range;
    using unpackrat::godot::container_kind;
    using unpackrat::godot::pack_location;
    using unpackrat::godot::pck_error;
    using unpackrat::godot::pck_index;

    std::string as_text(const godot_bytes &bytes)
    {
        return std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    }

    std::istringstream stream_of(const godot_bytes &bytes)
    {
        return std::istringstream(as_text(bytes));
    }

    std::optional<pack_location> locate(const godot_bytes &bytes)
    {
        auto input = stream_of(bytes);
        return unpackrat::godot::locate_pack(input);
    }

    pck_index read_at(const godot_bytes &bytes, const pack_location &location)
    {
        auto input = stream_of(bytes);
        return unpackrat::godot::read_pack(input, location);
    }

    pack_location standalone_of(const godot_bytes &bytes)
    {
        return pack_location{{0, bytes.size()}, container_kind::standalone};
    }

    std::vector<std::uint8_t> slice(const godot_bytes &bytes, const byte_range &range)
    {
        const auto begin = bytes.begin() + static_cast<std::ptrdiff_t>(range.offset);
        return std::vector<std::uint8_t>(begin, begin + static_cast<std::ptrdiff_t>(range.size));
    }

    godot_entry_options entry_with(std::string path, std::vector<std::uint8_t> payload, std::uint32_t flags = 0)
    {
        godot_entry_options entry;
        entry.path = std::move(path);
        entry.payload = std::move(payload);
        entry.flags = flags;
        return entry;
    }

    godot_pck_options single_entry_options(const std::uint32_t version)
    {
        godot_pck_options options;
        options.version = version;
        options.entries.push_back(entry_with("res://data/entry.bin", {'h', 'e', 'l', 'l', 'o'}));
        options.entries.back().md5 = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
        return options;
    }
} // namespace

TEST_CASE("godot pck: locate returns nullopt for inputs that are not packs")
{
    SECTION("empty input")
    {
        CHECK(!(locate(godot_bytes{}).has_value()));
    }
    SECTION("one byte")
    {
        CHECK(!(locate(godot_bytes{0x00}).has_value()));
    }
    SECTION("two bytes that are not a DOS image")
    {
        CHECK(!(locate(godot_bytes{'a', 'b'}).has_value()));
    }
    SECTION("three bytes")
    {
        CHECK(!(locate(godot_bytes{'a', 'b', 'c'}).has_value()));
    }
    SECTION("eight trailing bytes that are not a footer")
    {
        CHECK(!(locate(godot_bytes{'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'}).has_value()));
    }
}

TEST_CASE("godot pck: locates a standalone pack")
{
    const godot_bytes image = test::support::build_godot_pck(single_entry_options(4));
    const auto location = locate(image);
    REQUIRE(location.has_value());
    CHECK(location.value_or(pack_location{}).container == container_kind::standalone);
    CHECK(location.value_or(pack_location{}).pack.offset == 0);
    CHECK(location.value_or(pack_location{}).pack.size == image.size());
}

TEST_CASE("godot pck: rejects malformed PE candidates")
{
    SECTION("truncated DOS header")
    {
        CHECK_THROWS_AS(locate(godot_bytes{'M', 'Z'}), pck_error);
    }
    SECTION("out-of-range PE header offset")
    {
        godot_bytes bytes(0x40, 0);
        bytes[0] = 'M';
        bytes[1] = 'Z';
        test::support::patch_u32(bytes, 0x3C, 0xFFFFFFF0U);
        CHECK_THROWS_AS(locate(bytes), pck_error);
    }
    SECTION("PE header offset too close to the end of the file")
    {
        godot_bytes bytes(0x40, 0);
        bytes[0] = 'M';
        bytes[1] = 'Z';
        test::support::patch_u32(bytes, 0x3C, 0x30);
        CHECK_THROWS_AS(locate(bytes), pck_error);
    }
    SECTION("optional header larger than the file")
    {
        godot_pe_section section;
        section.name = "pck";
        section.optional_size = 0xFFFF;
        CHECK_THROWS_AS(locate(test::support::build_godot_pe(section)), pck_error);
    }
    SECTION("section table beyond the file")
    {
        godot_pe_section section;
        section.name = "pck";
        section.section_count = 0xFFFF;
        CHECK_THROWS_AS(locate(test::support::build_godot_pe(section)), pck_error);
    }
    SECTION("pck section pointing beyond the file")
    {
        godot_pe_section section;
        section.name = "pck";
        section.pointer = 0xF0000000U;
        CHECK_THROWS_AS(locate(test::support::build_godot_pe(section)), pck_error);
    }
    SECTION("pck section without a pack header")
    {
        godot_pe_section section;
        section.name = "pck";
        section.data.assign(16, 0);
        CHECK_THROWS_AS(locate(test::support::build_godot_pe(section)), pck_error);
    }
    SECTION("pck section too short to hold a header")
    {
        godot_pe_section section;
        section.name = "pck";
        section.data.assign(3, 0);
        CHECK_THROWS_AS(locate(test::support::build_godot_pe(section)), pck_error);
    }
}

TEST_CASE("godot pck: ignores PE candidates without a pack section")
{
    SECTION("DOS image without a PE signature")
    {
        godot_bytes bytes(0x60, 0);
        bytes[0] = 'M';
        bytes[1] = 'Z';
        test::support::patch_u32(bytes, 0x3C, 0x40);
        CHECK(!(locate(bytes).has_value()));
    }
    SECTION("section that is not named pck")
    {
        CHECK(!(locate(test::support::minimal_pe_without_pack()).has_value()));
    }
    SECTION("pck section with a zero pointer")
    {
        godot_pe_section section;
        section.name = "pck";
        section.pointer = 0;
        CHECK(!(locate(test::support::build_godot_pe(section)).has_value()));
    }
}

TEST_CASE("godot pck: locates an embedded pack in a PE section")
{
    SECTION("header at the section start")
    {
        const godot_bytes image = test::support::wrap_godot_pck_in_pe(single_entry_options(4));
        const auto location = locate(image);
        REQUIRE(location.has_value());
        CHECK(location.value_or(pack_location{}).container == container_kind::pe_section);
        REQUIRE(location.value_or(pack_location{}).pack.offset <= image.size());
        const auto offset = static_cast<std::size_t>(location.value_or(pack_location{}).pack.offset);
        REQUIRE(image.size() - offset >= 4);
        CHECK(test::support::godot_pck_magic ==
              (static_cast<std::uint32_t>(image[offset]) | (static_cast<std::uint32_t>(image[offset + 1]) << 8U) |
               (static_cast<std::uint32_t>(image[offset + 2]) << 16U) |
               (static_cast<std::uint32_t>(image[offset + 3]) << 24U)));
    }
    SECTION("header shifted by alignment padding")
    {
        const godot_bytes image = test::support::wrap_godot_pck_in_pe(single_entry_options(3), 3);
        const auto location = locate(image);
        REQUIRE(location.has_value());
        CHECK(location.value_or(pack_location{}).container == container_kind::pe_section);
        CHECK(location.value_or(pack_location{}).pack.offset % 4 == 3);
    }
}

TEST_CASE("godot pck: rejects a pack section whose declared range exceeds the file")
{
    SECTION("declared size extends past the end of the file")
    {
        godot_pe_section section;
        section.name = "pck";
        section.data = {0x47, 0x44, 0x50, 0x43};
        section.declared_size = 0x4000;
        CHECK_THROWS_AS(locate(test::support::build_godot_pe(section)), pck_error);
    }
    SECTION("valid section parses at exactly the declared size")
    {
        const godot_bytes image = test::support::wrap_godot_pck_in_pe(single_entry_options(4));
        const auto location = locate(image);
        REQUIRE(location.has_value());
        CHECK(location.value_or(pack_location{}).container == container_kind::pe_section);
        const pck_index index = read_at(image, location.value_or(pack_location{}));
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().path == "data/entry.bin");
    }
}

TEST_CASE("godot pck: locates a trailing-footer pack")
{
    const godot_bytes image = test::support::wrap_godot_pck_in_footer(single_entry_options(4), 0x40);
    const auto location = locate(image);
    REQUIRE(location.has_value());
    CHECK(location.value_or(pack_location{}).container == container_kind::footer);
    CHECK(location.value_or(pack_location{}).pack.offset == 0x40);
    CHECK(location.value_or(pack_location{}).pack.offset + location.value_or(pack_location{}).pack.size + 12 ==
          image.size());
}

TEST_CASE("godot pck: rejects malformed footers")
{
    SECTION("truncated footer")
    {
        CHECK_THROWS_AS(locate(godot_bytes{0x00, 0x47, 0x44, 0x50, 0x43}), pck_error);
    }
    SECTION("pack size below the minimum")
    {
        godot_bytes bytes(8, 0);
        test::support::patch_u64(bytes, 0, 2);
        bytes.push_back(0x47);
        bytes.push_back(0x44);
        bytes.push_back(0x50);
        bytes.push_back(0x43);
        CHECK_THROWS_AS(locate(bytes), pck_error);
    }
    SECTION("pack size beyond the file")
    {
        godot_bytes bytes(8, 0);
        test::support::patch_u64(bytes, 0, 0xFFFFFFFFU);
        bytes.push_back(0x47);
        bytes.push_back(0x44);
        bytes.push_back(0x50);
        bytes.push_back(0x43);
        CHECK_THROWS_AS(locate(bytes), pck_error);
    }
    SECTION("embedded header missing")
    {
        godot_bytes bytes(8, 0);
        test::support::patch_u64(bytes, 0, 8);
        bytes.push_back(0x47);
        bytes.push_back(0x44);
        bytes.push_back(0x50);
        bytes.push_back(0x43);
        CHECK_THROWS_AS(locate(bytes), pck_error);
    }
}

TEST_CASE("godot pck: reads every supported format version")
{
    for (const std::uint32_t version : {0U, 1U, 2U, 3U, 4U}) {
        SECTION(std::string("version ") + std::to_string(version))
        {
            godot_pck_options options = single_entry_options(version);
            options.engine_version = {3, 5, 2};
            const godot_bytes image = test::support::build_godot_pck(options);
            const pck_index index = read_at(image, standalone_of(image));
            CHECK(index.format_version == version);
            CHECK(index.engine_version == std::array<std::uint32_t, 3>{3, 5, 2});
            REQUIRE(index.entries.size() == 1);
            CHECK(index.entries.front().path == "data/entry.bin");
            CHECK(slice(image, index.entries.front().body) == std::vector<std::uint8_t>{'h', 'e', 'l', 'l', 'o'});
            CHECK(index.entries.front().md5 ==
                  std::array<std::byte, 16>{std::byte{1}, std::byte{2}, std::byte{3}, std::byte{4}, std::byte{5},
                                            std::byte{6}, std::byte{7}, std::byte{8}, std::byte{9}, std::byte{10},
                                            std::byte{11}, std::byte{12}, std::byte{13}, std::byte{14}, std::byte{15},
                                            std::byte{16}});
        }
    }
}

TEST_CASE("godot pck: strips the resource prefix and preserves unicode")
{
    godot_pck_options options;
    options.version = 4;
    const std::string path = std::string("res://") + "\xd0\xb4\xd0\xb0\xd0\xbd\xd0\xbd\xd1\x8b\xd0\xb5" + "/file.bin";
    options.entries.push_back(entry_with(path, {1, 2, 3}));
    const godot_bytes image = test::support::build_godot_pck(options);
    const pck_index index = read_at(image, standalone_of(image));
    REQUIRE(index.entries.size() == 1);
    CHECK(index.entries.front().path == std::string("\xd0\xb4\xd0\xb0\xd0\xbd\xd0\xbd\xd1\x8b\xd0\xb5") + "/file.bin");
}

TEST_CASE("godot pck: reads an empty pack and a zero-byte entry")
{
    SECTION("empty directory")
    {
        godot_pck_options options;
        options.version = 2;
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        CHECK(index.entries.empty());
    }
    SECTION("zero-byte entry")
    {
        godot_pck_options options;
        options.version = 4;
        options.entries.push_back(entry_with("empty.bin", {}));
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().body.size == 0);
    }
}

TEST_CASE("godot pck: honours a non-zero file base")
{
    SECTION("v3 relative base")
    {
        godot_pck_options options = single_entry_options(3);
        options.file_base = 0x20;
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(slice(image, index.entries.front().body) == std::vector<std::uint8_t>{'h', 'e', 'l', 'l', 'o'});
    }
    SECTION("v2 absolute base")
    {
        godot_pck_options options = single_entry_options(2);
        options.file_base = 0x20;
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(slice(image, index.entries.front().body) == std::vector<std::uint8_t>{'h', 'e', 'l', 'l', 'o'});
    }
    SECTION("v2 relative base flag")
    {
        godot_pck_options options = single_entry_options(2);
        options.pack_flags = test::support::godot_pack_flag_relative_base;
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().body.size == 5);
    }
}

TEST_CASE("godot pck: resolves embedded entry ranges")
{
    SECTION("PE section")
    {
        const godot_bytes image = test::support::wrap_godot_pck_in_pe(single_entry_options(4));
        const auto location = locate(image);
        REQUIRE(location.has_value());
        const pck_index index = read_at(image, location.value_or(pack_location{}));
        REQUIRE(index.entries.size() == 1);
        CHECK(slice(image, index.entries.front().body) == std::vector<std::uint8_t>{'h', 'e', 'l', 'l', 'o'});
    }
    SECTION("footer")
    {
        const godot_bytes image = test::support::wrap_godot_pck_in_footer(single_entry_options(4), 0x40);
        const auto location = locate(image);
        REQUIRE(location.has_value());
        const pck_index index = read_at(image, location.value_or(pack_location{}));
        REQUIRE(index.entries.size() == 1);
        CHECK(slice(image, index.entries.front().body) == std::vector<std::uint8_t>{'h', 'e', 'l', 'l', 'o'});
    }
}

TEST_CASE("godot pck: rejects unsupported versions and pack flags")
{
    SECTION("future format version")
    {
        const godot_bytes image = test::support::build_godot_pck(single_entry_options(5));
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("unknown pack flag")
    {
        godot_pck_options options = single_entry_options(2);
        options.pack_flags = 8;
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("encrypted directory")
    {
        godot_pck_options options = single_entry_options(2);
        options.pack_flags = 1;
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("sparse bundle")
    {
        godot_pck_options options = single_entry_options(2);
        options.pack_flags = 4;
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
}

TEST_CASE("godot pck: rejects unsupported entry modes and flags")
{
    SECTION("unknown entry flag")
    {
        godot_pck_options options = single_entry_options(2);
        options.entries.front().flags = 8;
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    for (const std::uint32_t flag : {1U, 2U, 4U}) {
        SECTION(std::string("mode flag ") + std::to_string(flag))
        {
            godot_pck_options options = single_entry_options(4);
            options.entries.front().flags = flag;
            const godot_bytes image = test::support::build_godot_pck(options);
            CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
        }
    }
}

TEST_CASE("godot pck: rejects an over-budget entry count")
{
    godot_pck_options options;
    options.version = 4;
    options.entry_count = 100001;
    const godot_bytes image = test::support::build_godot_pck(options);
    CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
}

TEST_CASE("godot pck: rejects unsafe entry names and paths")
{
    SECTION("empty name")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path.clear();
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("oversized name")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = std::string(4093, 'a');
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("embedded NUL")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = std::string("a\0b", 3);
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("absolute path")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = "/abs.bin";
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("traversal component")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = "a/../b.bin";
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("resource prefix without a remainder")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = "res://";
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("backslash separator")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = "a\\b.bin";
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("drive colon")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().path = "c:/x.bin";
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
}

TEST_CASE("godot pck: rejects out-of-range and overflowing entry bodies")
{
    SECTION("offset beyond the file")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().stored_offset = 0x1'0000'0000ULL;
        options.entries.front().stored_size = 0;
        options.entries.front().payload.clear();
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("size beyond the file")
    {
        godot_pck_options options = single_entry_options(4);
        options.entries.front().stored_size = 0xFFFF'FFFFULL;
        options.entries.front().payload.clear();
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("arithmetic overflow")
    {
        godot_pck_options options = single_entry_options(2);
        options.file_base = 0xFFFF'FFFF'FFFF'FFFFULL;
        options.entries.front().stored_offset = 5;
        options.entries.front().stored_size = 0;
        options.entries.front().payload.clear();
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
}

TEST_CASE("godot pck: rejects an out-of-range directory offset")
{
    SECTION("directory far beyond the file")
    {
        godot_pck_options options = single_entry_options(3);
        options.stored_directory_offset = 0xFFFF;
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("directory too close to the end of the file")
    {
        godot_pck_options options;
        options.version = 3;
        options.stored_directory_offset = 42;
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
}

TEST_CASE("godot pck: normalizes an unreadable stream")
{
    std::istringstream input("abcd");
    input.setstate(std::ios_base::badbit);
    CHECK_THROWS_AS(unpackrat::godot::locate_pack(input), pck_error);
}

TEST_CASE("godot pck: rejects a truncated directory and a bogus location")
{
    const godot_bytes image = test::support::build_godot_pck(single_entry_options(4));

    SECTION("truncated directory")
    {
        godot_bytes truncated = image;
        truncated.resize(38);
        CHECK_THROWS_AS(read_at(truncated, standalone_of(truncated)), pck_error);
    }
    SECTION("location offset beyond the file")
    {
        pack_location location = standalone_of(image);
        location.pack.offset = image.size() + 5;
        CHECK_THROWS_AS(read_at(image, location), pck_error);
    }
    SECTION("location size beyond the file")
    {
        pack_location location = standalone_of(image);
        location.pack.size = image.size() + 1;
        CHECK_THROWS_AS(read_at(image, location), pck_error);
    }
    SECTION("location magic missing")
    {
        pack_location location = standalone_of(image);
        location.pack.offset = 4;
        CHECK_THROWS_AS(read_at(image, location), pck_error);
    }
    SECTION("located range that does not begin with a pack header")
    {
        const godot_bytes not_a_pack(16, 0x5A);
        CHECK_THROWS_AS(read_at(not_a_pack, pack_location{{0, 16}, container_kind::standalone}), pck_error);
    }
}

TEST_CASE("godot pck: accepts officially NUL-padded entry names")
{
    SECTION("padded name round-trips with its body")
    {
        // "a.bin" is five bytes, so the official wire form pads it to eight stored bytes.
        godot_pck_options options;
        options.version = 4;
        options.entries.push_back(entry_with("a.bin", {'h', 'i'}));
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().path == "a.bin");
        CHECK(slice(image, index.entries.front().body) == std::vector<std::uint8_t>{'h', 'i'});
    }
    SECTION("unpadded historical name still parses")
    {
        godot_pck_options options;
        options.version = 1;
        godot_entry_options entry = entry_with("legacy.bin", {7});
        entry.raw_name = std::vector<std::uint8_t>{'l', 'e', 'g', 'a', 'c', 'y', '.', 'b', 'i', 'n'};
        options.entries.push_back(entry);
        const godot_bytes image = test::support::build_godot_pck(options);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().path == "legacy.bin");
    }
    SECTION("all-NUL name is rejected")
    {
        godot_pck_options options;
        options.version = 4;
        godot_entry_options entry = entry_with("ignored", {});
        entry.raw_name = std::vector<std::uint8_t>{0, 0, 0, 0};
        options.entries.push_back(entry);
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("NUL followed by a non-NUL is rejected as malformed padding")
    {
        godot_pck_options options;
        options.version = 4;
        godot_entry_options entry = entry_with("ignored", {});
        entry.raw_name = std::vector<std::uint8_t>{'a', 0, 'b', 0};
        options.entries.push_back(entry);
        const godot_bytes image = test::support::build_godot_pck(options);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
}

TEST_CASE("godot pck: bounds header and directory reads to the located pack range")
{
    const godot_bytes image = test::support::build_godot_pck(single_entry_options(4));

    SECTION("zero-sized location cannot hold a header")
    {
        CHECK_THROWS_AS(read_at(image, pack_location{{0, 0}, container_kind::standalone}), pck_error);
    }
    SECTION("location too small to hold a header")
    {
        CHECK_THROWS_AS(read_at(image, pack_location{{0, 4}, container_kind::standalone}), pck_error);
    }
    SECTION("directory crossing the pack end but inside the file")
    {
        pack_location location = standalone_of(image);
        location.pack.size = 42; // The v4 header is 40 bytes; the 4-byte count leaves the pack.
        CHECK_THROWS_AS(read_at(image, location), pck_error);
    }
    SECTION("directory offset inside the file but outside the pack")
    {
        const godot_bytes footer = test::support::wrap_godot_pck_in_footer(single_entry_options(4), 0x40);
        const auto located = locate(footer);
        REQUIRE(located.has_value());
        godot_bytes mutated = footer;
        // v4 stores the directory field 32 bytes into the pack; aim it just past the pack end.
        test::support::patch_u64(mutated, static_cast<std::size_t>(located.value_or(pack_location{}).pack.offset) + 32,
                                 located.value_or(pack_location{}).pack.size + 4);
        CHECK_THROWS_AS(read_at(mutated, located.value_or(pack_location{})), pck_error);
    }
}

TEST_CASE("godot pck: rejects a footer whose embedded start has no pack header")
{
    godot_bytes bytes(16, 0xAA);           // Sixteen filler bytes that are not a pack header.
    test::support::patch_u64(bytes, 8, 8); // The trailing footer reports an eight-byte payload.
    bytes.push_back(0x47);
    bytes.push_back(0x44);
    bytes.push_back(0x50);
    bytes.push_back(0x43);
    CHECK_THROWS_AS(locate(bytes), pck_error);
}

TEST_CASE("godot pck: rejects a directory too small for its declared entry count")
{
    godot_pck_options options;
    options.version = 4;
    options.entry_count = 100000; // A 44-byte v4 pack cannot hold this many entries.
    const godot_bytes image = test::support::build_godot_pck(options);
    REQUIRE(image.size() == 44);
    CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
}

TEST_CASE("godot pck: enforces the configurable index allocation budget")
{
    const std::uint64_t vector_bytes = sizeof(unpackrat::godot::pck_entry);

    SECTION("exact budget parses and one byte short rejects")
    {
        godot_pck_options options;
        options.version = 4;
        options.entries.push_back(entry_with("data/entry.bin", {'h', 'i'})); // 14 logical, 16 stored.
        const godot_bytes image = test::support::build_godot_pck(options);
        const std::uint64_t exact = vector_bytes + 16 + 14;

        unpackrat::godot::pck_limits limits;
        limits.max_index_bytes = exact;
        auto accepted = stream_of(image);
        const pck_index index = unpackrat::godot::read_pack(accepted, standalone_of(image), limits);
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().path == "data/entry.bin");

        limits.max_index_bytes = exact - 1;
        auto rejected = stream_of(image);
        CHECK_THROWS_AS(unpackrat::godot::read_pack(rejected, standalone_of(image), limits), pck_error);
    }
    SECTION("cumulative path bytes are budgeted across entries")
    {
        godot_pck_options options;
        options.version = 4;
        options.entries.push_back(entry_with("data/aa.bin", {'a'})); // 11 logical, 12 stored.
        options.entries.push_back(entry_with("data/bb.bin", {'b'})); // 11 logical, 12 stored.
        const godot_bytes image = test::support::build_godot_pck(options);
        const std::uint64_t exact = 2 * vector_bytes + (12 + 11) + (12 + 11);

        unpackrat::godot::pck_limits limits;
        limits.max_index_bytes = exact;
        auto accepted = stream_of(image);
        const pck_index index = unpackrat::godot::read_pack(accepted, standalone_of(image), limits);
        REQUIRE(index.entries.size() == 2);

        limits.max_index_bytes = exact - 1;
        auto rejected = stream_of(image);
        CHECK_THROWS_AS(unpackrat::godot::read_pack(rejected, standalone_of(image), limits), pck_error);
    }
}

TEST_CASE("godot pck: normalizes an unreadable stream while reading a directory")
{
    std::istringstream input("abcd");
    input.setstate(std::ios_base::badbit);
    CHECK_THROWS_AS(unpackrat::godot::read_pack(input, pack_location{{0, 4}, container_kind::standalone}), pck_error);
}

TEST_CASE("godot pck: enforces the logical path limit on raw unpadded names")
{
    // The stored name-length field may carry up to three trailing NUL padding bytes, so it
    // tolerates max_path_bytes + 3. A raw, unpadded name has no padding to trim, so its logical
    // length must still respect the max_path_bytes cap. The fixture's raw_name writes exactly
    // these bytes without the official padded encoding.
    std::size_t sequence = 0;
    const auto raw_name_image = [&sequence](const std::size_t length) {
        godot_pck_options options;
        options.version = 4;
        godot_entry_options entry = entry_with("f" + std::to_string(sequence++) + ".bin", {'x'});
        entry.raw_name = std::vector<std::uint8_t>(length, static_cast<std::uint8_t>('a'));
        options.entries.push_back(entry);
        return test::support::build_godot_pck(options);
    };

    SECTION("exactly max_path_bytes raw name is accepted")
    {
        constexpr std::size_t limit = unpackrat::archive_limits::max_path_bytes;
        const godot_bytes image = raw_name_image(limit);
        const pck_index index = read_at(image, standalone_of(image));
        REQUIRE(index.entries.size() == 1);
        CHECK(index.entries.front().path == std::string(limit, 'a'));
    }
    SECTION("max_path_bytes + 1 raw name is rejected")
    {
        const godot_bytes image = raw_name_image(unpackrat::archive_limits::max_path_bytes + 1);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("max_path_bytes + 2 raw name is rejected")
    {
        const godot_bytes image = raw_name_image(unpackrat::archive_limits::max_path_bytes + 2);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
    SECTION("max_path_bytes + 3 raw name is rejected")
    {
        const godot_bytes image = raw_name_image(unpackrat::archive_limits::max_path_bytes + 3);
        CHECK_THROWS_AS(read_at(image, standalone_of(image)), pck_error);
    }
}
