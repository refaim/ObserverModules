#include "../../core/godot/encrypted_payload.h"
#include "../../core/godot/pck.h"
#include "../support/godot_crypto_fixtures.h"
#include "../support/godot_fixtures.h"

#include "../../core/archive_limits.h"

#include <algorithm>
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

TEST_CASE("godot pck: decrypts bounded directories and preserves member transport ranges")
{
    const auto key = test::support::godot_test_key();
    for (const std::uint32_t version : {2U, 3U, 4U}) {
        for (const bool directory : {false, true}) {
            for (const bool member : {false, true}) {
                CAPTURE(version, directory, member);
                const auto bytes = test::support::encrypted_pck(version, directory, member);
                auto input = stream_of(bytes);
                const auto index = unpackrat::godot::read_pack(input, standalone_of(bytes), {}, &key);
                REQUIRE(index.entries.size() == 1);
                const auto &entry = index.entries.front();
                CHECK(entry.path == "data.bin");
                CHECK(entry.logical_size == 5);
                CHECK(entry.encrypted == member);
                CHECK(entry.kind == unpackrat::godot::pck_entry_kind::file);
                if (member) {
                    CHECK(entry.body.size == 56);
                    const auto payload = unpackrat::godot::read_encrypted_payload(input, entry.body);
                    unpackrat::godot::verified_payload_source decoded(input, payload, key);
                    decoded.verify();
                    std::string actual(5, '\0');
                    decoded.stream().read(actual.data(), 5);
                    CHECK(actual == "hello");
                } else {
                    CHECK(entry.body.size == 5);
                    CHECK(slice(bytes, entry.body) == godot_bytes{'h', 'e', 'l', 'l', 'o'});
                }
                if (directory) {
                    auto missing = stream_of(bytes);
                    CHECK_THROWS_AS(unpackrat::godot::read_pack(missing, standalone_of(bytes)),
                                    unpackrat::godot::pck_key_required);
                    auto wrong_key = key;
                    wrong_key[0] ^= std::byte{1};
                    auto wrong = stream_of(bytes);
                    CHECK_THROWS_AS(unpackrat::godot::read_pack(wrong, standalone_of(bytes), {}, &wrong_key),
                                    unpackrat::godot::key_mismatch);
                }
            }
        }
    }
}

TEST_CASE("godot pck: encrypted member header must agree with the directory logical size")
{
    auto bytes = test::support::encrypted_pck(3, false, true);
    constexpr std::size_t member_length_field = 40 + 4 + 4 + 16 + 8;
    test::support::patch_u64(bytes, member_length_field, 4);
    CHECK_THROWS_AS(read_at(bytes, standalone_of(bytes)), pck_error);
}

TEST_CASE("godot pck: encrypted member cannot consume ciphertext beyond its located pack",
          "[godot][encrypted-pack-boundary]")
{
    for (const std::uint32_t version : {3U, 4U}) {
        CAPTURE(version);
        godot_pe_section section;
        section.data = test::support::encrypted_pck(version, false, true);
        // The directory and the declared five logical bytes fit; the remaining encrypted
        // envelope is physically present only in the PE overlay, outside the claimed section.
        section.declared_size = 105;
        const auto bytes = test::support::build_godot_pe(section);
        const auto location = locate(bytes);
        REQUIRE(location.has_value());
        CHECK(location.value_or(pack_location{}).pack.offset + location.value_or(pack_location{}).pack.size <
              bytes.size());
        CHECK_THROWS_AS(read_at(bytes, location.value_or(pack_location{})), pck_error);
    }
}

TEST_CASE("godot pck: encrypted member cannot consume an envelope before its located pack",
          "[godot][encrypted-pack-boundary]")
{
    godot_pe_section section;
    section.data = test::support::encrypted_pck(2, false, true);
    constexpr std::size_t stored_offset_field = 96 + 4 + 4 + 16;
    constexpr std::size_t original_envelope = 96 + 4 + 56;
    constexpr std::size_t prefix_envelope = 64;
    test::support::patch_u64(section.data, stored_offset_field, prefix_envelope);
    auto bytes = test::support::build_godot_pe(section);
    // An independently valid envelope in unused DOS-header padding must not become pack data.
    std::copy_n(section.data.begin() + original_envelope, 56, bytes.begin() + prefix_envelope);
    const auto location = locate(bytes);
    REQUIRE(location.has_value());
    CHECK_THROWS_AS(read_at(bytes, location.value_or(pack_location{})), pck_error);
}

TEST_CASE("godot pck: mixed encrypted removal flags are rejected explicitly")
{
    auto options = single_entry_options(3);
    options.entries.front().payload.clear();
    options.entries.front().flags = 3;
    const auto bytes = test::support::build_godot_pck(options);
    CHECK_THROWS_AS(read_at(bytes, standalone_of(bytes)), pck_error);
}

TEST_CASE("godot pck: encrypted directory respects the aggregate index budget")
{
    const auto bytes = test::support::encrypted_pck(3, true, false);
    const auto key = test::support::godot_test_key();
    auto input = stream_of(bytes);
    unpackrat::godot::pck_limits limits;
    limits.max_index_bytes = 32;
    CHECK_THROWS_AS(unpackrat::godot::read_pack(input, standalone_of(bytes), limits, &key), pck_error);
}

TEST_CASE("godot pck: encrypted directory count must consume the verified plaintext exactly",
          "[godot][encrypted-directory-consumption]")
{
    const auto key = test::support::godot_test_key();
    for (const std::uint32_t version : {2U, 3U, 4U}) {
        CAPTURE(version);
        auto bytes = test::support::encrypted_pck(version, true, false);
        // The count precedes the encrypted envelope, so this change leaves its MD5 valid.
        test::support::patch_u32(bytes, version == 2 ? 96 : 40, 0);
        auto input = stream_of(bytes);
        CHECK_THROWS_AS(unpackrat::godot::read_pack(input, standalone_of(bytes), {}, &key), pck_error);
    }
}

TEST_CASE("godot pck: missing-key error reports the exact candidate validation byte cost")
{
    const auto bytes = test::support::encrypted_pck(3, true, false);
    auto input = stream_of(bytes);
    try {
        static_cast<void>(unpackrat::godot::read_pack(input, standalone_of(bytes)));
        FAIL("encrypted nonempty directory accepted without a key");
    } catch (const unpackrat::godot::pck_key_required &error) {
        CHECK(error.validation_bytes == 64);
    }
}

TEST_CASE("godot pck: an empty encrypted directory needs no key and is not key evidence")
{
    godot_pck_options options;
    options.version = 3;
    options.pack_flags = 1;
    auto bytes = test::support::build_godot_pck(options);
    const auto envelope = test::support::encrypted_envelope({});
    bytes.insert(bytes.end(), envelope.begin(), envelope.end());
    const auto index = read_at(bytes, standalone_of(bytes));
    CHECK(index.entries.empty());
    CHECK(!index.key_evidence);
}

TEST_CASE("godot pck: recognizes zero-size removal records without treating them as files")
{
    auto options = single_entry_options(3);
    options.entries.push_back(entry_with("res://gone.txt", {}, 2));
    const auto bytes = test::support::build_godot_pck(options);
    const auto index = read_at(bytes, standalone_of(bytes));
    REQUIRE(index.entries.size() == 2);
    CHECK(index.entries[0].kind == unpackrat::godot::pck_entry_kind::file);
    CHECK(index.entries[1].kind == unpackrat::godot::pck_entry_kind::removal);
    CHECK(index.entries[1].logical_size == 0);
    CHECK(index.entries[1].body.size == 0);
}

TEST_CASE("godot pck: canonical zero PE placeholder permits footer fallback")
{
    godot_pe_section section;
    section.data.resize(512, 0);
    auto bytes = test::support::build_godot_pe(section);
    // PE32+ section header begins at 0x80 + 24 + 0xF0; Godot reserves an eight-byte symbol.
    test::support::patch_u32(bytes, 0x188 + 8, 8);
    CHECK(!locate(bytes).has_value());
    auto options = single_entry_options(3);
    options.image_offset = bytes.size();
    const auto pack = test::support::build_godot_pck(options);
    bytes.insert(bytes.end(), pack.begin(), pack.end());
    const std::size_t footer = bytes.size();
    bytes.resize(footer + 12);
    test::support::patch_u64(bytes, footer, pack.size());
    test::support::patch_u32(bytes, footer + 8, test::support::godot_pck_magic);
    const auto location = locate(bytes);
    REQUIRE(location.has_value());
    CHECK(location.value_or(pack_location{}).container == container_kind::footer);
    CHECK(read_at(bytes, location.value_or(pack_location{})).entries.size() == 1);
    bytes[512] = 1;
    CHECK_THROWS_AS(locate(bytes), pck_error);
}

TEST_CASE("godot pck: a noncanonical zero PE section remains a malformed pack claim")
{
    godot_pe_section section;
    section.data.resize(16, 0);
    auto bytes = test::support::build_godot_pe(section);
    test::support::patch_u32(bytes, 0x188 + 8, 8);
    CHECK_THROWS_AS(locate(bytes), pck_error);
}
