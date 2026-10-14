#include "../../core/codecs/crypto_codec.h"
#include "../../core/godot/encrypted_payload.h"
#include "../support/godot_crypto_fixtures.h"
#include "../support/godot_texture_fixtures.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using namespace unpackrat::godot;
    using test::support::godot_bytes;

    std::istringstream source_of(const godot_bytes &bytes)
    {
        return std::istringstream(std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size()));
    }

    std::vector<std::byte> from_hex(const std::string_view hex)
    {
        const auto digit = [](const char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
        std::vector<std::byte> output;
        for (std::size_t index = 0; index < hex.size(); index += 2) {
            output.push_back(static_cast<std::byte>((static_cast<unsigned>(digit(hex[index])) << 4U) |
                                                    static_cast<unsigned>(digit(hex[index + 1]))));
        }
        return output;
    }
} // namespace

TEST_CASE("godot crypto: AES-256-CFB128 matches the NIST SP800-38A vector across partial chunks")
{
    const auto key_bytes = from_hex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4");
    const auto iv_bytes = from_hex("000102030405060708090a0b0c0d0e0f");
    const auto cipher = from_hex("dc7e84bfda79164b7ecd8486985d386039ffed143b28b1c832113c6331e5407b");
    const auto expected = from_hex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51");
    unpackrat::codecs::key256 key{};
    unpackrat::codecs::iv128 iv{};
    std::copy(key_bytes.begin(), key_bytes.end(), key.begin());
    std::copy(iv_bytes.begin(), iv_bytes.end(), iv.begin());
    unpackrat::codecs::aes256_cfb128 decoder(key, iv);
    std::vector<std::byte> actual(cipher.size());
    decoder.decrypt(std::span(cipher).first(7), std::span(actual).first(7));
    decoder.decrypt(std::span(cipher).subspan(7), std::span(actual).subspan(7));
    CHECK(actual == expected);
    decoder.reset(iv);
    decoder.decrypt(cipher, actual);
    CHECK(actual == expected);
    CHECK_THROWS_AS(decoder.decrypt(cipher, std::span(actual).first(1)), unpackrat::codecs::crypto_error);
    unpackrat::codecs::erase_key(key);
    CHECK(std::all_of(key.begin(), key.end(), [](const std::byte byte) { return byte == std::byte{0}; }));
}

TEST_CASE("godot crypto: incremental MD5 matches an independent known digest")
{
    const auto bytes = from_hex("616263");
    unpackrat::codecs::md5_state state;
    state.update(std::span(bytes).first(1));
    state.update(std::span(bytes).subspan(1));
    const auto digest = state.finish();
    const auto expected = from_hex("900150983cd24fb0d6963f7d28e17f72");
    CHECK(std::equal(digest.begin(), digest.end(), expected.begin(), expected.end()));
}

TEST_CASE("godot encryption: a verified source preserves logical bytes and random access")
{
    for (const std::size_t length : std::array<std::size_t, 6>{0, 1, 15, 16, 17, 65537}) {
        CAPTURE(length);
        godot_bytes plaintext(length);
        for (std::size_t index = 0; index < length; ++index) {
            plaintext[index] = static_cast<std::uint8_t>(index);
        }
        const godot_bytes bytes = test::support::encrypted_envelope(plaintext);
        auto input = source_of(bytes);
        const auto payload = read_encrypted_payload(input, {0, bytes.size()});
        CHECK(payload.logical_size == length);
        CHECK(payload.envelope.size == bytes.size());
        CHECK(payload.ciphertext.offset == 40);
        CHECK(payload.key_evidence() == (length != 0));
        const auto key = test::support::godot_test_key();
        verified_payload_source source(input, payload, key);
        CHECK(!source.verified());
        if (length != 0) {
            CHECK(source.unverified_stream().get() == plaintext[0]);
        }
        CHECK_THROWS_AS(source.stream(), encryption_error);
        std::uint64_t checked = 0;
        source.verify([&checked](const std::uint64_t amount) { checked += amount; });
        CHECK(source.verified());
        CHECK(checked == length);
        auto &decoded = source.stream();
        decoded.seekg(0, std::ios::end);
        CHECK(decoded.tellg() == static_cast<std::streamoff>(length));
        decoded.seekg(0);
        std::string actual(length, '\0');
        decoded.read(actual.data(), static_cast<std::streamsize>(length));
        CHECK(actual == std::string(reinterpret_cast<const char *>(plaintext.data()), plaintext.size()));
        if (length > 1) {
            decoded.seekg(1);
            std::string tail(length - 1, '\0');
            decoded.read(tail.data(), static_cast<std::streamsize>(tail.size()));
            CHECK(tail == actual.substr(1));
            decoded.seekg(1);
            CHECK(decoded.peek() == plaintext[1]);
            decoded.seekg(-1, std::ios::end);
            CHECK(decoded.get() == plaintext.back());
            decoded.seekg(0, std::ios::beg);
            decoded.seekg(1, std::ios::cur);
            CHECK(decoded.get() == plaintext[1]);
        }
        decoded.clear();
        decoded.seekg(static_cast<std::streamoff>(length));
        CHECK(decoded.get() == std::char_traits<char>::eof());
        decoded.clear();
        decoded.seekg(-1);
        CHECK(decoded.fail());
        decoded.clear();
        decoded.seekg(static_cast<std::streamoff>(length + 1));
        CHECK(decoded.fail());
    }
}

TEST_CASE("godot encryption: invalid envelopes fail before decrypting or allocating plaintext")
{
    const auto bytes = test::support::encrypted_envelope(godot_bytes{1, 2, 3});
    SECTION("truncated header")
    {
        auto input = source_of(bytes);
        CHECK_THROWS_AS(read_encrypted_payload(input, {0, 39}), encryption_error);
    }
    SECTION("source window past EOF")
    {
        auto input = source_of(bytes);
        CHECK_THROWS_AS(read_encrypted_payload(input, {1, bytes.size()}), encryption_error);
    }
    SECTION("ciphertext truncated")
    {
        auto input = source_of(bytes);
        CHECK_THROWS_AS(read_encrypted_payload(input, {0, bytes.size() - 1}), encryption_error);
    }
    SECTION("length overflow")
    {
        auto malformed = bytes;
        test::support::patch_u64(malformed, 16, (std::numeric_limits<std::uint64_t>::max)());
        auto input = source_of(malformed);
        CHECK_THROWS_AS(read_encrypted_payload(input, {0, malformed.size()}), encryption_error);
    }
    SECTION("logical budget")
    {
        auto input = source_of(bytes);
        encryption_limits limits;
        limits.max_plaintext_bytes = 2;
        CHECK_THROWS_AS(read_encrypted_payload(input, {0, bytes.size()}, limits), encryption_error);
    }
    SECTION("offset overflow")
    {
        auto input = source_of(bytes);
        CHECK_THROWS_AS(read_encrypted_payload(input, {(std::numeric_limits<std::uint64_t>::max)(), 1}),
                        encryption_error);
    }
    SECTION("source refuses reads")
    {
        test::support::unreadable_stream input(static_cast<std::streamoff>(bytes.size()));
        CHECK_THROWS_AS(read_encrypted_payload(input, {0, bytes.size()}), encryption_error);
    }
}

TEST_CASE("godot encryption: embedded windows preserve offsets and reject source changes")
{
    const auto envelope = test::support::encrypted_envelope(godot_bytes{1, 2, 3, 4});
    godot_bytes bytes(7, 0xcc);
    bytes.insert(bytes.end(), envelope.begin(), envelope.end());
    bytes.insert(bytes.end(), 5, 0xaa);
    auto input = source_of(bytes);
    const auto payload = read_encrypted_payload(input, {7, bytes.size() - 7});
    CHECK(payload.envelope.offset == 7);
    CHECK(payload.envelope.size == envelope.size());
    const auto key = test::support::godot_test_key();
    verified_payload_source source(input, payload, key);
    source.verify();
    auto &view = source.stream();
    CHECK(view.get() == 1);
    CHECK(view.rdbuf()->pubseekoff(0, std::ios::beg, std::ios::out) == std::streampos(-1));
    input.str(std::string(7, 'x'));
    CHECK_THROWS_AS(source.verify(), encryption_error);
    CHECK(!source.verified());
    CHECK_THROWS_AS(source.stream(), encryption_error);
}

TEST_CASE("godot encryption: a small structural probe does not prefetch the member")
{
    const auto bytes = test::support::encrypted_envelope(godot_bytes(65537, 0x33));
    test::support::counting_stream input(std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size()));
    const auto payload = read_encrypted_payload(input, {0, bytes.size()});
    const auto key = test::support::godot_test_key();
    verified_payload_source source(input, payload, key);
    const auto before = input.bytes_read();
    std::array<char, 16> header{};
    CHECK(source.unverified_stream().rdbuf()->sgetn(header.data(), 0) == 0);
    CHECK(source.unverified_stream().rdbuf()->sgetn(header.data(), -1) == 0);
    source.unverified_stream().read(header.data(), static_cast<std::streamsize>(header.size()));
    CHECK(input.bytes_read() - before == header.size());
    CHECK(std::all_of(header.begin(), header.end(), [](const char c) { return c == 0x33; }));
    source.unverified_stream().seekg(17);
    const auto before_peek = input.bytes_read();
    CHECK(source.unverified_stream().peek() == 0x33);
    CHECK(input.bytes_read() - before_peek == 18);
}

TEST_CASE("godot encryption: sequential metadata reads reuse the CFB cursor without extra source reads",
          "[godot][encryption][buffered-read]")
{
    godot_bytes plaintext(256);
    for (std::size_t index = 0; index < plaintext.size(); ++index) {
        plaintext[index] = static_cast<std::uint8_t>(index);
    }
    const auto bytes = test::support::encrypted_envelope(plaintext);
    test::support::counting_stream input(std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size()));
    const auto payload = read_encrypted_payload(input, {0, bytes.size()});
    verified_payload_source source(input, payload, test::support::godot_test_key());
    const auto before = input.bytes_read();
    auto &view = source.unverified_stream();
    std::array<char, 4> chunk{};
    for (std::size_t offset = 0; offset < plaintext.size(); offset += chunk.size()) {
        view.read(chunk.data(), static_cast<std::streamsize>(chunk.size()));
        REQUIRE(view.gcount() == static_cast<std::streamsize>(chunk.size()));
        for (std::size_t index = 0; index < chunk.size(); ++index) {
            CHECK(static_cast<std::uint8_t>(chunk[index]) == plaintext[offset + index]);
        }
    }
    CHECK(input.bytes_read() - before == plaintext.size());
}

TEST_CASE("godot encryption: seeks inside the last decrypted window reuse it without source reads",
          "[godot][encryption][buffered-seek]")
{
    godot_bytes plaintext(64);
    for (std::size_t index = 0; index < plaintext.size(); ++index) {
        plaintext[index] = static_cast<std::uint8_t>(index);
    }
    const auto bytes = test::support::encrypted_envelope(plaintext);
    test::support::counting_stream input(std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size()));
    const auto payload = read_encrypted_payload(input, {0, bytes.size()});
    verified_payload_source source(input, payload, test::support::godot_test_key());
    const auto before = input.bytes_read();
    auto &view = source.unverified_stream();
    std::array<char, 16> first{};
    view.read(first.data(), static_cast<std::streamsize>(first.size()));
    REQUIRE(view.gcount() == static_cast<std::streamsize>(first.size()));
    CHECK(input.bytes_read() - before == first.size());

    view.seekg(4);
    std::array<char, 4> repeated{};
    view.read(repeated.data(), static_cast<std::streamsize>(repeated.size()));
    REQUIRE(view.gcount() == static_cast<std::streamsize>(repeated.size()));
    for (std::size_t index = 0; index < repeated.size(); ++index) {
        CHECK(static_cast<std::uint8_t>(repeated[index]) == plaintext[4 + index]);
    }
    CHECK(input.bytes_read() - before == first.size());

    view.seekg(16);
    view.read(repeated.data(), static_cast<std::streamsize>(repeated.size()));
    REQUIRE(view.gcount() == static_cast<std::streamsize>(repeated.size()));
    for (std::size_t index = 0; index < repeated.size(); ++index) {
        CHECK(static_cast<std::uint8_t>(repeated[index]) == plaintext[16 + index]);
    }
    CHECK(input.bytes_read() - before == first.size() + repeated.size());
}

TEST_CASE("godot encryption: padding is outside the logical digest")
{
    auto bytes = test::support::encrypted_envelope(godot_bytes{1, 2, 3});
    bytes.back() ^= 0xFFU;
    auto input = source_of(bytes);
    const auto key = test::support::godot_test_key();
    verified_payload_source source(input, read_encrypted_payload(input, {0, bytes.size()}), key);
    source.verify();
    std::array<char, 3> actual{};
    source.stream().read(actual.data(), 3);
    CHECK(actual == std::array<char, 3>{1, 2, 3});
}

TEST_CASE("godot encryption: complete MD5 rejects wrong keys and corruption")
{
    for (const bool wrong_key : {false, true}) {
        auto bytes = test::support::encrypted_envelope(godot_bytes{1, 2, 3, 4});
        auto key = test::support::godot_test_key();
        if (wrong_key) {
            key[0] ^= std::byte{1};
        } else {
            bytes[40] ^= 1U;
        }
        auto input = source_of(bytes);
        verified_payload_source source(input, read_encrypted_payload(input, {0, bytes.size()}), key);
        CHECK_THROWS_AS(source.verify(), key_mismatch);
        CHECK_THROWS_AS(source.stream(), encryption_error);
    }
}

TEST_CASE("godot encryption: verification propagates cancellation")
{
    const auto bytes = test::support::encrypted_envelope(godot_bytes{1, 2, 3});
    auto input = source_of(bytes);
    const auto key = test::support::godot_test_key();
    verified_payload_source source(input, read_encrypted_payload(input, {0, bytes.size()}), key);
    CHECK_THROWS_AS(source.verify([](std::uint64_t) { throw std::runtime_error("cancel"); }), std::runtime_error);
    CHECK_THROWS_AS(source.stream(), encryption_error);
    source.verify();
    CHECK(source.stream().get() == 1);
}
