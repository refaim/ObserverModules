#include "../../core/codecs/resource_compression.h"
#include "../../core/godot/rscc.h"
#include "../support/compression_fixtures.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

#ifdef _DEBUG
#include <brotli/decode.h>
#include <crtdbg.h>
#include <zlib.h>
#define ZSTD_STATIC_LINKING_ONLY
#include <zstd.h>
#endif

#include <catch2/catch_test_macros.hpp>

namespace
{
    using unpackrat::codecs::block_stream_decoder;
    using unpackrat::codecs::compression_error;
    using unpackrat::codecs::compression_kind;
    using unpackrat::codecs::decompress_block;
    using unpackrat::godot::decompress_rscc;
    using unpackrat::godot::rscc_error;
    using unpackrat::godot::rscc_limits;
    using unpackrat::godot::rscc_logical_size;
    using unpackrat::godot::rscc_metadata_reader;

    constexpr std::array<compression_kind, 5> all_modes{compression_kind::fastlz, compression_kind::deflate,
                                                        compression_kind::zstd, compression_kind::gzip,
                                                        compression_kind::brotli};

    std::vector<std::byte> rscc_magic()
    {
        return {std::byte{'R'}, std::byte{'S'}, std::byte{'C'}, std::byte{'C'}};
    }

    std::vector<std::byte> rscc_header(const std::uint32_t mode, const std::uint32_t block_size,
                                       const std::uint32_t total)
    {
        std::vector<std::byte> output = rscc_magic();
        const std::array<std::uint32_t, 3> fields{mode, block_size, total};
        for (const auto value : fields) {
            for (int shift = 0; shift < 32; shift += 8) {
                output.push_back(static_cast<std::byte>((value >> shift) & 0xFFU));
            }
        }
        return output;
    }

    void append_u32(std::vector<std::byte> &output, const std::uint32_t value)
    {
        for (int shift = 0; shift < 32; shift += 8) {
            output.push_back(static_cast<std::byte>((value >> shift) & 0xFFU));
        }
    }

    void append_magic(std::vector<std::byte> &output)
    {
        const auto magic = rscc_magic();
        output.insert(output.end(), magic.begin(), magic.end());
    }

    void corrupt_last_byte(std::vector<std::byte> &bytes)
    {
        bytes.back() ^= std::byte{0xFF};
    }

    std::vector<std::byte> byte_stream(std::initializer_list<int> values)
    {
        std::vector<std::byte> output;
        output.reserve(values.size());
        for (const int value : values) {
            output.push_back(static_cast<std::byte>(value & 0xFF));
        }
        return output;
    }

    std::vector<std::byte> repeat(std::vector<std::byte> prefix, const std::size_t count, const int value)
    {
        prefix.insert(prefix.end(), count, static_cast<std::byte>(value & 0xFF));
        return prefix;
    }

    // Rejects a malformed FastLZ block and requires the adapter to report the stream as
    // truncated. These streams carry a structurally valid 32 byte literal prefix that a
    // too-small expected output bound would reject first, masking the token level defect
    // the walk is meant to reach.
    void require_fastlz_truncated(const std::vector<std::byte> &stream, const std::size_t expected_size)
    {
        try {
            static_cast<void>(decompress_block(stream, compression_kind::fastlz, expected_size));
        } catch (const compression_error &error) {
            REQUIRE(std::string(error.what()).find("truncated") != std::string::npos);
            return;
        }
        FAIL("expected a malformed fastlz block to be rejected as truncated");
    }

    // A structurally valid level-2 FastLZ stream that exercises the tokens the reference
    // decoder accepts but the compact encoder rarely produces for our fixtures: an all
    // literal prefix long enough to reach a far distance, a length escape with a 0xFF
    // continuation byte, a real far 16-bit distance, and a final near match whose low
    // offset byte is 0xFF while the control high-offset bits are not 31. Every token
    // copies the literal byte, so the whole payload is that byte.
    std::vector<std::byte> crafted_level2_stream()
    {
        std::vector<std::byte> out;
        out.push_back(std::byte{0x3F}); // level 2 marker bit set, literal run of 32
        out.insert(out.end(), 32, std::byte{0x41});
        for (int chunk = 0; chunk < 255; ++chunk) {
            out.push_back(std::byte{0x1F}); // literal run of 32
            out.insert(out.end(), 32, std::byte{0x41});
        }
        // Length escape: control 0xE0 selects the escape, 0xFF continues, 0x00 ends it.
        out.push_back(std::byte{0xE0});
        out.push_back(std::byte{0xFF});
        out.push_back(std::byte{0x00});
        out.push_back(std::byte{0x01}); // low offset -> copy 264 bytes from op - 2
        // Far match: control 0x5F (high bits 31) with low byte 0xFF selects the 16-bit
        // distance 0x0102, so the match copies 4 bytes from op - 8450.
        out.push_back(std::byte{0x5F});
        out.push_back(std::byte{0xFF});
        out.push_back(std::byte{0x01});
        out.push_back(std::byte{0x02});
        // Near match with low byte 0xFF but high bits 1 -> copy 3 bytes from op - 512.
        out.push_back(std::byte{0x21});
        out.push_back(std::byte{0xFF});
        return out;
    }

    // A structurally valid level 2 stream whose length escape drives the reference
    // decoder's 32-bit accumulator past its limit. 0x20 starts a literal run of one,
    // 0xE0 selects the escape, 16,843,009 copies of 0xFF add 2^32 - 1, and the 0x07
    // terminator closes the escape. The total wraps so the logical match length of
    // 2^32 + 15 collapses to 15, and the library copies one literal plus a 15 byte match
    // and reports exactly the 16 byte padded block the caller asked for.
    std::vector<std::byte> wrapped_level2_escape()
    {
        std::vector<std::byte> out;
        out.reserve(16843014);
        out.push_back(std::byte{0x20}); // level 2 marker, literal run of one
        out.push_back(std::byte{0x41});
        out.push_back(std::byte{0xE0}); // length escape: 7 - 1 selects the escape loop
        out.insert(out.end(), 16843009, std::byte{0xFF});
        out.push_back(std::byte{0x07}); // terminates the escape; the accumulator wraps
        out.push_back(std::byte{0x00}); // offset byte
        return out;
    }
} // namespace

TEST_CASE("resource compression: round trips every mode with exact output")
{
    const std::array<std::size_t, 5> sizes{1, 8, 16, 64, std::size_t{200} * 1024};
    for (const auto kind : all_modes) {
        for (const auto size : sizes) {
            const auto payload = test::support::make_payload(size);
            const auto compressed = test::support::compress_block(payload, kind);
            REQUIRE(decompress_block(compressed, kind, payload.size()) == payload);
        }
    }
}

TEST_CASE("resource compression: rejects input and output beyond the budget")
{
    const auto payload = test::support::make_payload(8);
    const auto fastlz = test::support::compress_block(payload, compression_kind::fastlz);
    REQUIRE(fastlz.size() > payload.size());
    REQUIRE_THROWS_AS(decompress_block(fastlz, compression_kind::fastlz, payload.size(), fastlz.size() - 1),
                      compression_error);

    const auto zstd = test::support::compress_block(test::support::make_payload(64), compression_kind::zstd);
    REQUIRE(zstd.size() < 64);
    REQUIRE_THROWS_AS(decompress_block(zstd, compression_kind::zstd, 64, zstd.size()), compression_error);
}

TEST_CASE("resource compression: rejects an unknown mode")
{
    const auto payload = test::support::make_payload(4);
    // Inject a representable but unsupported wire mode to verify rejection.
    // NOLINTNEXTLINE(clang-analyzer-optin.core.EnumCastOutOfRange)
    REQUIRE_THROWS_AS(decompress_block(payload, static_cast<compression_kind>(99), 0), compression_error);
}

TEST_CASE("resource compression: fastlz exact output, short padding and rejection")
{
    const auto payload = test::support::make_payload(32);
    const auto compressed = test::support::compress_block(payload, compression_kind::fastlz);
    REQUIRE(decompress_block(compressed, compression_kind::fastlz, 32) == payload);

    const auto short_payload = test::support::make_payload(8);
    const auto short_compressed = test::support::compress_block(short_payload, compression_kind::fastlz);
    REQUIRE(decompress_block(short_compressed, compression_kind::fastlz, 8) == short_payload);

    const auto empty_compressed = test::support::compress_block({}, compression_kind::fastlz);
    REQUIRE(decompress_block(empty_compressed, compression_kind::fastlz, 0).empty());

    REQUIRE_THROWS_AS(decompress_block(compressed, compression_kind::fastlz, 64), compression_error);

    constexpr std::array<std::byte, 1> unknown_level{std::byte{0x40}};
    REQUIRE_THROWS_AS(decompress_block(unknown_level, compression_kind::fastlz, 32), compression_error);
    REQUIRE_THROWS_AS(decompress_block(unknown_level, compression_kind::fastlz, 8), compression_error);
    REQUIRE_THROWS_AS(decompress_block({}, compression_kind::fastlz, 0), compression_error);
}

TEST_CASE("resource compression: fastlz short blocks require the exact padded length")
{
    // A one-literal stream decodes a single byte; asked for a short logical block it must
    // not be accepted as an eight byte block whose tail is fabricated padding.
    const auto one_literal = byte_stream({0x00, 0x41});
    REQUIRE_THROWS_AS(decompress_block(one_literal, compression_kind::fastlz, 8), compression_error);
    REQUIRE_THROWS_AS(decompress_block(one_literal, compression_kind::fastlz, 1), compression_error);

    // The canonical writer pads a short block to the 16 byte FastLZ minimum before
    // compressing, so a real short block decodes to exactly 16 bytes.
    const auto padded = repeat(byte_stream({0x0F}), 16, 0x41);
    REQUIRE(decompress_block(padded, compression_kind::fastlz, 16) == std::vector<std::byte>(16, std::byte{0x41}));
}

TEST_CASE("resource compression: fastlz requires the whole block to be consumed")
{
    // A well formed 16 literal block followed by one arbitrary byte: the reference decoder
    // stops before the last byte, so only an independent consumption check rejects it.
    const auto trailing = repeat(byte_stream({0x0F}), 16, 0x41);
    auto with_junk = trailing;
    with_junk.push_back(std::byte{0x7A});
    REQUIRE_THROWS_AS(decompress_block(with_junk, compression_kind::fastlz, 16), compression_error);

    // The same bytes without the trailing byte are a valid block.
    REQUIRE(decompress_block(trailing, compression_kind::fastlz, 16) == std::vector<std::byte>(16, std::byte{0x41}));

    // A control byte that begins a match but is missing its offset byte is truncated.
    const auto missing_offset = byte_stream({0x02, 0x41, 0x42, 0x43, 0x20});
    REQUIRE_THROWS_AS(decompress_block(missing_offset, compression_kind::fastlz, 16), compression_error);
}

TEST_CASE("resource compression: fastlz validates level 1 and level 2 token streams")
{
    // Well formed level 1 stream of thirteen literals then a short back reference: it
    // expands to exactly the 16 byte padded block, so it is accepted.
    auto level1_match = byte_stream({0x0C});
    level1_match = repeat(std::move(level1_match), 13, 0x41);
    level1_match.push_back(std::byte{0x20});
    level1_match.push_back(std::byte{0x00});
    REQUIRE(decompress_block(level1_match, compression_kind::fastlz, 16) ==
            std::vector<std::byte>(16, std::byte{0x41}));

    // Level 1 length escape: three literals then a control 0xE0, one length byte and one
    // offset byte, expanding to the 16 byte padded block.
    const auto level1_escape = byte_stream({0x02, 0x41, 0x41, 0x41, 0xE0, 0x04, 0x02});
    REQUIRE(decompress_block(level1_escape, compression_kind::fastlz, 16) ==
            std::vector<std::byte>(16, std::byte{0x41}));

    // A length escape whose length byte would fall outside the block is truncated even
    // though the trailing offset byte is physically present.
    const auto level1_escape_truncated = byte_stream({0x02, 0x41, 0x42, 0x43, 0xE0, 0x00});
    REQUIRE_THROWS_AS(decompress_block(level1_escape_truncated, compression_kind::fastlz, 16), compression_error);

    // A back reference before the start of the output is rejected by the reference decoder.
    const auto level1_bad_reference = byte_stream({0x02, 0x41, 0x42, 0x43, 0x20, 0x05});
    REQUIRE_THROWS_AS(decompress_block(level1_bad_reference, compression_kind::fastlz, 16), compression_error);
    REQUIRE_THROWS_AS(decompress_block(level1_bad_reference, compression_kind::fastlz, 8), compression_error);

    // Level 2 tokens, including a length escape, a far 16-bit distance and a near match
    // whose low offset byte is 0xFF. The whole payload is the 0x41 literal.
    const auto level2 = crafted_level2_stream();
    REQUIRE(decompress_block(level2, compression_kind::fastlz, 8463) == std::vector<std::byte>(8463, std::byte{0x41}));

    // Level 2 literal run truncated before its bytes.
    REQUIRE_THROWS_AS(decompress_block(byte_stream({0x3F, 0x41}), compression_kind::fastlz, 16), compression_error);

    // Level 2 length escape truncated before any length byte.
    const auto level2_escape_truncated = repeat(byte_stream({0x3F}), 32, 0x41);
    auto l2_escape = level2_escape_truncated;
    l2_escape.push_back(std::byte{0xE0});
    require_fastlz_truncated(l2_escape, 32);

    // Level 2 match truncated before its offset byte.
    auto l2_offset = level2_escape_truncated;
    l2_offset.push_back(std::byte{0x21});
    require_fastlz_truncated(l2_offset, 32);

    // Level 2 far match truncated before its extended distance bytes.
    auto l2_far = level2_escape_truncated;
    l2_far.push_back(std::byte{0x5F});
    l2_far.push_back(std::byte{0xFF});
    l2_far.push_back(std::byte{0x00});
    require_fastlz_truncated(l2_far, 32);
}

TEST_CASE("resource compression: fastlz rejects a wrapped level 2 length escape")
{
    // The escape's continuation bytes carry a logical match length of more than 2^32, but
    // the reference decoder accumulates them into a 32-bit counter that wraps to a short
    // 15 byte match and reports exactly the requested 16 byte block. The adapter must
    // reject the escape before the library ever sees it instead of trusting that size.
    const auto stream = wrapped_level2_escape();
    REQUIRE_THROWS_AS(decompress_block(stream, compression_kind::fastlz, 16), compression_error);
}

TEST_CASE("resource compression: fastlz rejects tokens that expand past the expected output")
{
    // Two 16 byte literal runs decode to 32 bytes, twice the 16 byte block the stream is
    // asked to produce, so the running output total must be rejected before the library.
    auto two_runs = repeat(byte_stream({0x0F}), 16, 0x41);
    two_runs = repeat(std::move(two_runs), 1, 0x0F);
    two_runs = repeat(std::move(two_runs), 16, 0x41);
    REQUIRE_THROWS_AS(decompress_block(two_runs, compression_kind::fastlz, 16), compression_error);
}

TEST_CASE("resource compression: zlib streams enforce exact output, trailing and truncation")
{
    const std::array<compression_kind, 2> modes{compression_kind::deflate, compression_kind::gzip};
    for (const auto kind : modes) {
        const auto payload = test::support::make_payload(std::size_t{200} * 1024);
        const auto compressed = test::support::compress_block(payload, kind);
        REQUIRE(decompress_block(compressed, kind, payload.size()) == payload);

        auto trailing = compressed;
        trailing.push_back(std::byte{0});
        REQUIRE_THROWS_AS(decompress_block(trailing, kind, payload.size()), compression_error);

        auto truncated = compressed;
        truncated.pop_back();
        REQUIRE_THROWS_AS(decompress_block(truncated, kind, payload.size()), compression_error);

        REQUIRE_THROWS_AS(decompress_block(compressed, kind, payload.size() + 1), compression_error);
        REQUIRE_THROWS_AS(decompress_block(compressed, kind, payload.size() - 1), compression_error);

        auto corrupted = compressed;
        corrupted[corrupted.size() / 2] ^= std::byte{0xFF};
        REQUIRE_THROWS_AS(decompress_block(corrupted, kind, payload.size()), compression_error);
    }

    constexpr std::array<std::byte, 6> needs_dictionary{std::byte{0x78}, std::byte{0xbb}, std::byte{0},
                                                        std::byte{0},    std::byte{0},    std::byte{1}};
    REQUIRE_THROWS_AS(decompress_block(needs_dictionary, compression_kind::deflate, 0), compression_error);
}

TEST_CASE("resource compression: zstd enforces frames and exact output")
{
    const auto payload = test::support::make_payload(64);
    const auto compressed = test::support::compress_block(payload, compression_kind::zstd);
    REQUIRE(decompress_block(compressed, compression_kind::zstd, payload.size()) == payload);

    auto trailing = compressed;
    trailing.push_back(std::byte{0});
    REQUIRE_THROWS_AS(decompress_block(trailing, compression_kind::zstd, payload.size()), compression_error);
    REQUIRE_THROWS_AS(decompress_block(compressed, compression_kind::zstd, payload.size() + 1), compression_error);
    REQUIRE_THROWS_AS(decompress_block(compressed, compression_kind::zstd, 8), compression_error);

    constexpr std::array<std::byte, 4> garbage{std::byte{1}, std::byte{2}, std::byte{3}, std::byte{4}};
    REQUIRE_THROWS_AS(decompress_block(garbage, compression_kind::zstd, 4), compression_error);

    const auto empty_compressed = test::support::compress_block({}, compression_kind::zstd);
    REQUIRE(decompress_block(empty_compressed, compression_kind::zstd, 0).empty());
}

TEST_CASE("resource compression: brotli enforces exact output, trailing and truncation")
{
    const auto payload = test::support::make_payload(std::size_t{200} * 1024);
    const auto compressed = test::support::compress_block(payload, compression_kind::brotli);
    REQUIRE(decompress_block(compressed, compression_kind::brotli, payload.size()) == payload);

    auto trailing = compressed;
    trailing.push_back(std::byte{0});
    REQUIRE_THROWS_AS(decompress_block(trailing, compression_kind::brotli, payload.size()), compression_error);

    auto truncated = compressed;
    truncated.resize(truncated.size() / 2);
    REQUIRE_THROWS_AS(decompress_block(truncated, compression_kind::brotli, payload.size()), compression_error);

    REQUIRE_THROWS_AS(decompress_block(compressed, compression_kind::brotli, payload.size() + 1), compression_error);
    REQUIRE_THROWS_AS(decompress_block(compressed, compression_kind::brotli, payload.size() - 1), compression_error);

    constexpr std::array<std::byte, 4> invalid{std::byte{0x9f}, std::byte{0xff}, std::byte{0xff}, std::byte{0xff}};
    REQUIRE_THROWS_AS(decompress_block(invalid, compression_kind::brotli, 0), compression_error);

    // A stream that opens with an unset ISLAST, the reserved nibble count (0b11) and a set
    // reserved bit is structurally invalid, so the decoder reports a hard format error
    // instead of "needs more input".
    constexpr std::array<std::byte, 1> reserved_format{std::byte{0x1C}};
    REQUIRE_THROWS_AS(decompress_block(reserved_format, compression_kind::brotli, 0), compression_error);

    const auto empty_compressed = test::support::compress_block({}, compression_kind::brotli);
    REQUIRE(decompress_block(empty_compressed, compression_kind::brotli, 0).empty());
}

TEST_CASE("rscc: round trips logical payloads")
{
    struct case_spec final
    {
        std::size_t logical_bytes;
        std::uint32_t block_size;
    };
    const std::array<case_spec, 4> specs{{{0, 16}, {20, 16}, {32, 16}, {5000, 1024}}};
    for (const auto kind : all_modes) {
        for (const auto &spec : specs) {
            const auto payload = test::support::make_payload(spec.logical_bytes);
            const auto stream = test::support::build_rscc(payload, kind, spec.block_size);
            REQUIRE(decompress_rscc(stream) == payload);
        }
    }
}

TEST_CASE("rscc: metadata reader rejects bytes not accounted for by its block table",
          "[godot][rscc-metadata][trailing-table]")
{
    const auto payload = test::support::make_payload(10);
    auto stored = test::support::build_rscc(payload, compression_kind::deflate, 16);
    stored.insert(stored.end() - 4, std::byte{0x7F});
    const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        std::memcpy(target.data(), stored.data() + offset, target.size());
    };
    CHECK_THROWS_AS(rscc_metadata_reader(stored.size(), source), rscc_error);
}
TEST_CASE("rscc: metadata reader fetches only requested blocks and supports backward seeks", "[godot][rscc-metadata]")
{
    const auto payload = test::support::make_payload(40);
    for (const auto kind : all_modes) {
        CAPTURE(kind);
        const auto stored = test::support::build_rscc(payload, kind, 16);
        std::size_t fetched = 0;
        const auto source = [&stored, &fetched](const std::uint64_t offset, const std::span<std::byte> target) {
            if (offset > stored.size() || target.size() > stored.size() - offset) {
                throw std::logic_error("test source read outside RSCC");
            }
            fetched += target.size();
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        rscc_metadata_reader reader(stored.size(), source);
        CHECK(reader.logical_size() == payload.size());
        std::array<std::byte, 6> across{};
        reader.read(14, across);
        CHECK(std::equal(across.begin(), across.end(), payload.begin() + 14));
        std::array<std::byte, 4> back{};
        REQUIRE_NOTHROW(reader.read(2, back));
        CHECK(std::equal(back.begin(), back.end(), payload.begin() + 2));
        std::array<std::byte, 10> tail{};
        REQUIRE_NOTHROW(reader.read(30, tail));
        CHECK(std::equal(tail.begin(), tail.end(), payload.begin() + 30));
        CHECK_THROWS_AS(reader.read(40, back), rscc_error);
        CHECK(fetched < stored.size() * 3);
    }
}

TEST_CASE("rscc: metadata reader rejects bounded envelope and touched-block faults", "[godot][rscc-metadata]")
{
    const auto payload = test::support::make_payload(40);
    const auto valid = test::support::build_rscc(payload, compression_kind::deflate, 16);
    const auto reader_for = [](const std::vector<std::byte> &stored, const rscc_limits &limits = {}) {
        const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            if (offset > stored.size() || target.size() > stored.size() - offset) {
                throw std::logic_error("test source read outside RSCC");
            }
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        return rscc_metadata_reader(stored.size(), source, limits);
    };

    CHECK_THROWS_AS(reader_for(std::vector<std::byte>(19)), rscc_error);
    auto bad_header = valid;
    bad_header[0] = std::byte{'X'};
    CHECK_THROWS_AS(reader_for(bad_header), rscc_error);
    auto bad_trailer = valid;
    bad_trailer.back() = std::byte{'X'};
    CHECK_THROWS_AS(reader_for(bad_trailer), rscc_error);
    auto zero_block = valid;
    zero_block[8] = std::byte{0};
    zero_block[9] = std::byte{0};
    CHECK_THROWS_AS(reader_for(zero_block), rscc_error);
    auto bad_mode = valid;
    bad_mode[4] = std::byte{5};
    CHECK_THROWS_AS(reader_for(bad_mode), rscc_error);
    rscc_limits tiny_input;
    tiny_input.max_input_bytes = 8;
    CHECK_THROWS_AS(reader_for(valid, tiny_input), rscc_error);
    rscc_limits tiny_output;
    tiny_output.max_output_bytes = 8;
    CHECK_THROWS_AS(reader_for(valid, tiny_output), rscc_error);
    rscc_limits few_blocks;
    few_blocks.max_blocks = 1;
    CHECK_THROWS_AS(reader_for(valid, few_blocks), rscc_error);
    auto short_table = valid;
    short_table.resize(20);
    const auto magic = rscc_magic();
    std::copy(magic.begin(), magic.end(), short_table.end() - 4);
    CHECK_THROWS_AS(reader_for(short_table), rscc_error);

    std::array<std::byte, 1> result{};
    auto reader = reader_for(valid);
    rscc_limits tiny_block;
    tiny_block.max_block_bytes = 1;
    auto block_limited = reader_for(valid, tiny_block);
    CHECK_THROWS_AS(block_limited.read(0, result), rscc_error);
    auto corrupt = valid;
    corrupt[28] = std::byte{0xFF};
    auto corrupt_reader = reader_for(corrupt);
    CHECK_THROWS_AS(corrupt_reader.read(0, result), rscc_error);
    CHECK_THROWS_AS(reader.read(41, result), rscc_error);
}

TEST_CASE("rscc: metadata-only prefix read does not materialize a small media block",
          "[godot][rscc-metadata][prefix-only]")
{
    const auto payload = test::support::make_payload(std::size_t{1024} * 1024);
    const auto stored = test::support::build_rscc(payload, compression_kind::deflate, 1024U * 1024U);
    REQUIRE(stored.size() > std::size_t{256} * 1024);
    std::uint64_t fetched = 0;
    const auto source = [&stored, &fetched](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        fetched += target.size();
        std::memcpy(target.data(), stored.data() + offset, target.size());
    };
    rscc_metadata_reader reader(stored.size(), source);
    REQUIRE(reader.logical_size() == payload.size());
    std::array<std::byte, 64> prefix{};
    reader.read(0, prefix);
    CHECK(std::equal(prefix.begin(), prefix.end(), payload.begin()));
    CHECK(fetched < stored.size() / 2);
}

TEST_CASE("rscc: distant metadata reads batch table entries without touching skipped blocks",
          "[godot][rscc-metadata][table-batch]")
{
    constexpr std::uint32_t block_size = 16;
    constexpr std::uint32_t full_blocks = 1302;
    constexpr std::uint32_t far_index = 1300;
    constexpr std::uint32_t back_index = 3;
    const auto compress_fill = [](const std::byte value) {
        return test::support::compress_block(std::vector<std::byte>(block_size, value), compression_kind::deflate);
    };
    const auto common = compress_fill(std::byte{0x11});
    const auto far = compress_fill(std::byte{0xB2});
    const auto back = compress_fill(std::byte{0xA3});
    const auto terminal = test::support::compress_block({}, compression_kind::deflate);
    const auto block_for = [&](const std::uint32_t index) -> const std::vector<std::byte> & {
        if (index == far_index)
            return far;
        if (index == back_index)
            return back;
        return index == full_blocks ? terminal : common;
    };
    auto stored =
        rscc_header(static_cast<std::uint32_t>(compression_kind::deflate), block_size, full_blocks * block_size);
    for (std::uint32_t index = 0; index <= full_blocks; ++index)
        append_u32(stored, static_cast<std::uint32_t>(block_for(index).size()));
    const std::uint64_t data_start = stored.size();
    std::uint64_t far_start = 0;
    std::uint64_t back_start = 0;
    for (std::uint32_t index = 0; index <= full_blocks; ++index) {
        if (index == far_index)
            far_start = stored.size();
        if (index == back_index)
            back_start = stored.size();
        const auto &block = block_for(index);
        stored.insert(stored.end(), block.begin(), block.end());
    }
    const std::uint64_t data_end = stored.size();
    append_magic(stored);

    std::size_t table_reads = 0;
    std::size_t payload_reads = 0;
    bool only_target_payload = true;
    std::uint64_t expected_start = 0;
    std::uint64_t expected_end = 0;
    const auto source = [&](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        if (offset >= 16 && offset < data_start)
            ++table_reads;
        if (offset >= data_start && offset < data_end) {
            ++payload_reads;
            only_target_payload = only_target_payload && offset >= expected_start && offset <= expected_end &&
                                  target.size() <= expected_end - offset;
        }
        std::memcpy(target.data(), stored.data() + offset, target.size());
    };
    rscc_metadata_reader reader(stored.size(), source);
    table_reads = 0;
    payload_reads = 0;

    expected_start = far_start;
    expected_end = far_start + far.size();
    std::array<std::byte, 4> output{};
    reader.read(static_cast<std::uint64_t>(far_index) * block_size + 5, output);
    CHECK(output == std::array<std::byte, 4>{std::byte{0xB2}, std::byte{0xB2}, std::byte{0xB2}, std::byte{0xB2}});
    CHECK(table_reads <= 2);
    CHECK(payload_reads > 0);
    CHECK(only_target_payload);

    table_reads = 0;
    payload_reads = 0;
    only_target_payload = true;
    expected_start = back_start;
    expected_end = back_start + back.size();
    reader.read(static_cast<std::uint64_t>(back_index) * block_size + 5, output);
    CHECK(output == std::array<std::byte, 4>{std::byte{0xA3}, std::byte{0xA3}, std::byte{0xA3}, std::byte{0xA3}});
    CHECK(table_reads <= 1);
    CHECK(payload_reads > 0);
    CHECK(only_target_payload);
}

TEST_CASE("rscc: metadata reader checks block table extents and compressed-byte limit", "[godot][rscc-metadata]")
{
    const auto payload = test::support::make_payload(16);
    const auto valid = test::support::build_rscc(payload, compression_kind::deflate, 16);
    const auto source_for = [](const std::vector<std::byte> &stored) {
        return [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
    };
    std::array<std::byte, 1> output{};
    auto too_far = valid;
    too_far[16] = too_far[17] = too_far[18] = too_far[19] = std::byte{0xFF};
    CHECK_THROWS_AS(rscc_metadata_reader(too_far.size(), source_for(too_far)), rscc_error);

    const std::uint32_t compressed =
        std::to_integer<std::uint32_t>(valid[16]) | std::to_integer<std::uint32_t>(valid[17]) << 8U |
        std::to_integer<std::uint32_t>(valid[18]) << 16U | std::to_integer<std::uint32_t>(valid[19]) << 24U;
    REQUIRE(compressed > payload.size());
    rscc_limits bound;
    bound.max_block_bytes = compressed - 1;
    rscc_metadata_reader over_budget(valid.size(), source_for(valid), bound);
    CHECK_THROWS_AS(over_budget.read(0, output), rscc_error);
}

TEST_CASE("rscc: metadata reader streams a corrupt wide block and a valid final partial block",
          "[godot][rscc-metadata]")
{
    constexpr std::uint32_t mib = 1024U * 1024U;
    rscc_limits wide;
    wide.max_block_bytes = (std::numeric_limits<std::uint32_t>::max)();
    const auto source_for = [](const std::vector<std::byte> &stored) {
        return [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
    };
    SECTION("corrupt wide compressed data reports an RSCC format error")
    {
        const std::vector<std::byte> payload(std::size_t{17} * mib, std::byte{0x42});
        auto stored = test::support::build_rscc(payload, compression_kind::deflate, 17U * mib);
        REQUIRE(stored.size() > 28);
        stored[24] = std::byte{0};
        rscc_metadata_reader reader(stored.size(), source_for(stored), wide);
        std::array<std::byte, 1> output{};
        CHECK_THROWS_AS(reader.read(0, output), rscc_error);
    }
    SECTION("the last partial block can itself exceed 16 MiB")
    {
        const std::vector<std::byte> payload(std::size_t{41} * mib, std::byte{0x42});
        const auto stored = test::support::build_rscc(payload, compression_kind::deflate, 24U * mib);
        rscc_metadata_reader reader(stored.size(), source_for(stored), wide);
        REQUIRE(reader.logical_size() == payload.size());
        std::array<std::byte, 4> output{};
        reader.read(std::uint64_t{24} * mib, output);
        CHECK(output == std::array<std::byte, 4>{std::byte{0x42}, std::byte{0x42}, std::byte{0x42}, std::byte{0x42}});
    }
}
TEST_CASE("resource compression: bounded block stream matches level-1 and far-distance level-2 FastLZ",
          "[godot][block-stream]")
{
    auto level1 = byte_stream({0x0C});
    level1 = repeat(std::move(level1), 13, 0x41);
    level1.push_back(std::byte{0x20});
    level1.push_back(std::byte{0x00});
    const std::array streams{level1, crafted_level2_stream()};
    const std::array<std::size_t, 2> lengths{16, 8463};
    for (std::size_t index = 0; index < streams.size(); ++index) {
        const auto &stored = streams[index];
        const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        block_stream_decoder decoder(compression_kind::fastlz, stored.size(), lengths[index], source);
        std::vector<std::byte> result(lengths[index]);
        decoder.read(0, result);
        CHECK(result == std::vector<std::byte>(lengths[index], std::byte{0x41}));
        std::array<std::byte, 3> back{};
        decoder.read(1, back);
        CHECK(back == std::array<std::byte, 3>{std::byte{0x41}, std::byte{0x41}, std::byte{0x41}});
    }
}

TEST_CASE("resource compression: empty random reads at logical EOF do not decode a valid block",
          "[godot][block-stream][empty-range]")
{
    const auto payload = test::support::make_payload(100000);
    for (const compression_kind kind : all_modes) {
        CAPTURE(kind);
        const auto stored = test::support::compress_block(payload, kind);
        std::size_t source_calls = 0;
        const auto source = [&stored, &source_calls](const std::uint64_t offset, const std::span<std::byte> target) {
            ++source_calls;
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        block_stream_decoder decoder(kind, stored.size(), payload.size(), source);
        decoder.read(payload.size(), {});
        CHECK(source_calls == 0);
        std::array<std::byte, 4> first{};
        decoder.read(0, first);
        CHECK(std::equal(first.begin(), first.end(), payload.begin()));
        const std::size_t before = source_calls;
        decoder.read(payload.size(), {});
        CHECK(source_calls == before);
    }
}
TEST_CASE("resource compression: streaming decoder rejects malformed FastLZ tokens and unknown modes",
          "[godot][block-stream]")
{
    const auto malformed = [](const std::vector<std::byte> &stored, const std::uint64_t logical) {
        const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        block_stream_decoder reader(compression_kind::fastlz, stored.size(), logical, source);
        std::array<std::byte, 1> output{};
        CHECK_THROWS_AS(reader.read(0, output), compression_error);
    };
    malformed({std::byte{0x00}}, 1);
    malformed({std::byte{0xE0}, std::byte{0x00}}, 1);
    malformed({std::byte{0x00}, std::byte{0x41}, std::byte{0x21}, std::byte{0xFF}}, 5);
    malformed({std::byte{0x20}, std::byte{0x41}, std::byte{0xE0}, std::byte{0xFF}}, 1);

    const auto no_source = [](std::uint64_t, std::span<std::byte>) {
        FAIL("invalid mode must be rejected before reading a source");
    };
    const auto invalid_mode = std::bit_cast<compression_kind>(std::uint32_t{99});
    CHECK_THROWS_AS(block_stream_decoder(invalid_mode, 0, 0, no_source), compression_error);
}

TEST_CASE("resource compression: streaming zstd and brotli reject malformed compressed bytes", "[godot][block-stream]")
{
    const std::vector<std::byte> stored{std::byte{0xFF}, std::byte{0x00}};
    const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        std::memcpy(target.data(), stored.data() + offset, target.size());
    };
    for (const auto kind : {compression_kind::zstd, compression_kind::brotli}) {
        CAPTURE(kind);
        block_stream_decoder reader(kind, stored.size(), 1, source);
        std::array<std::byte, 1> output{};
        CHECK_THROWS_AS(reader.read(0, output), compression_error);
    }
}
TEST_CASE("resource compression: bounded block stream validates terminal length and trailing data",
          "[godot][block-stream]")
{
    const std::vector<std::byte> payload(65536, std::byte{0x41});
    for (const compression_kind kind : all_modes) {
        CAPTURE(kind);
        auto stored = test::support::compress_block(payload, kind);
        stored.push_back(std::byte{0x7F});
        const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        block_stream_decoder decoder(kind, stored.size(), payload.size(), source);
        std::vector<std::byte> result(payload.size());
        CHECK_THROWS_AS(decoder.read(0, result), compression_error);
    }
}

TEST_CASE("resource compression: bounded block stream supports window crossings, backward seeks and failures",
          "[godot][block-stream]")
{
    const auto payload = test::support::make_payload(std::size_t{200} * 1024);
    for (const compression_kind kind : all_modes) {
        CAPTURE(kind);
        const auto stored = test::support::compress_block(payload, kind);
        const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= stored.size());
            REQUIRE(target.size() <= stored.size() - offset);
            std::memcpy(target.data(), stored.data() + offset, target.size());
        };
        block_stream_decoder decoder(kind, stored.size(), payload.size(), source);
        std::array<std::byte, 20> crossing{};
        decoder.read(65530, crossing);
        CHECK(std::equal(crossing.begin(), crossing.end(), payload.begin() + 65530));
        std::array<std::byte, 17> first{};
        decoder.read(0, first);
        CHECK(std::equal(first.begin(), first.end(), payload.begin()));
        std::vector<std::byte> full(payload.size());
        decoder.read(0, full);
        CHECK(full == payload);
        CHECK_THROWS_AS(decoder.read(payload.size(), first), compression_error);

        block_stream_decoder too_short(kind, stored.size(), payload.size() - 1, source);
        CHECK_THROWS_AS(too_short.read(0, std::span<std::byte>(full.data(), full.size() - 1)), compression_error);
        block_stream_decoder too_long(kind, stored.size(), payload.size() + 1, source);
        std::vector<std::byte> expanded(payload.size() + 1);
        CHECK_THROWS_AS(too_long.read(0, expanded), compression_error);

        const std::span<const std::byte> truncated(stored.data(), stored.size() - 1);
        const auto short_source = [truncated](const std::uint64_t offset, const std::span<std::byte> target) {
            REQUIRE(offset <= truncated.size());
            REQUIRE(target.size() <= truncated.size() - offset);
            std::memcpy(target.data(), truncated.data() + offset, target.size());
        };
        block_stream_decoder clipped(kind, truncated.size(), payload.size(), short_source);
        CHECK_THROWS_AS(clipped.read(0, full), compression_error);
    }
}

TEST_CASE("rscc: a caller output budget wider than size_t still decodes a small payload")
{
    const auto payload = test::support::make_payload(40);
    const auto stream = test::support::build_rscc(payload, compression_kind::zstd, 16);
    rscc_limits generous;
    generous.max_output_bytes = (std::numeric_limits<std::uint64_t>::max)();

    REQUIRE(decompress_rscc(stream, generous) == payload);
    if constexpr (sizeof(std::size_t) == 4) {
        // A direct uint64_t-to-size_t cast would turn this budget into zero.
        generous.max_output_bytes = 0x1'0000'0000ULL;
        REQUIRE(decompress_rscc(stream, generous) == payload);
    }
}

TEST_CASE("rscc: rejects malformed containers")
{
    const auto payload = test::support::make_payload(40);
    const auto valid = test::support::build_rscc(payload, compression_kind::zstd, 16);
    REQUIRE(decompress_rscc(valid) == payload);

    REQUIRE_THROWS_AS(decompress_rscc(std::vector<std::byte>(15)), rscc_error);

    auto bad_header = valid;
    bad_header[0] = std::byte{'X'};
    REQUIRE_THROWS_AS(decompress_rscc(bad_header), rscc_error);

    auto bad_trailer = valid;
    corrupt_last_byte(bad_trailer);
    REQUIRE_THROWS_AS(decompress_rscc(bad_trailer), rscc_error);

    auto zero_block = rscc_header(2, 0, 16);
    append_magic(zero_block);
    REQUIRE_THROWS_AS(decompress_rscc(zero_block), rscc_error);

    auto unknown_mode = rscc_header(5, 16, 0);
    append_magic(unknown_mode);
    REQUIRE_THROWS_AS(decompress_rscc(unknown_mode), rscc_error);

    auto huge_count = rscc_header(2, 1, 100000);
    append_magic(huge_count);
    REQUIRE_THROWS_AS(decompress_rscc(huge_count), rscc_error);

    auto short_table = rscc_header(2, 16, 64);
    short_table.resize(26);
    const auto magic = rscc_magic();
    std::copy(magic.begin(), magic.end(), short_table.end() - 4);
    REQUIRE_THROWS_AS(decompress_rscc(short_table), rscc_error);

    auto oversized_block = rscc_header(2, 16, 16);
    append_u32(oversized_block, 32 * 1024 * 1024);
    append_u32(oversized_block, 0);
    append_magic(oversized_block);
    REQUIRE_THROWS_AS(decompress_rscc(oversized_block), rscc_error);

    auto past_data = rscc_header(2, 16, 16);
    append_u32(past_data, 8);
    append_u32(past_data, 8);
    past_data.resize(past_data.size() + 4);
    append_magic(past_data);
    REQUIRE_THROWS_AS(decompress_rscc(past_data), rscc_error);

    auto extra_padding = valid;
    extra_padding.insert(extra_padding.end() - 4, std::byte{0});
    REQUIRE_THROWS_AS(decompress_rscc(extra_padding), rscc_error);

    rscc_limits tiny_input;
    tiny_input.max_input_bytes = 8;
    REQUIRE_THROWS_AS(decompress_rscc(valid, tiny_input), rscc_error);

    rscc_limits tiny_output;
    tiny_output.max_output_bytes = 8;
    REQUIRE_THROWS_AS(decompress_rscc(valid, tiny_output), rscc_error);

    rscc_limits few_blocks;
    few_blocks.max_blocks = 1;
    REQUIRE_THROWS_AS(decompress_rscc(valid, few_blocks), rscc_error);
}

TEST_CASE("rscc: translates a codec failure into rscc_error")
{
    // A structurally valid container whose single deflate block declares a preset
    // dictionary. The block codec raises compression_error, which must be translated
    // into rscc_error instead of leaking the codec's own error type.
    constexpr std::array<std::byte, 6> needs_dictionary{std::byte{0x78}, std::byte{0xbb}, std::byte{0},
                                                        std::byte{0},    std::byte{0},    std::byte{1}};
    auto container = rscc_header(1, 16, 8);
    append_u32(container, static_cast<std::uint32_t>(needs_dictionary.size()));
    container.insert(container.end(), needs_dictionary.begin(), needs_dictionary.end());
    append_magic(container);
    REQUIRE_THROWS_AS(decompress_rscc(container), rscc_error);
}

TEST_CASE("rscc: rejects a fastlz block with a wrapped length escape")
{
    // The same wrapped escape inside a real "RSCC" container: the adapter must reject the
    // block instead of accepting the spoofed exact output size, so the container fails.
    const auto block = wrapped_level2_escape();
    auto container = rscc_header(0, 17, 16); // fastlz, a single block of 16 logical bytes
    append_u32(container, static_cast<std::uint32_t>(block.size()));
    container.insert(container.end(), block.begin(), block.end());
    append_magic(container);

    rscc_limits generous;
    generous.max_block_bytes = 32ULL * 1024ULL * 1024ULL;
    REQUIRE_THROWS_AS(decompress_rscc(container, generous), rscc_error);
}

TEST_CASE("rscc: reports the declared logical size before any decode")
{
    struct case_spec final
    {
        std::size_t logical_bytes;
        std::uint32_t block_size;
    };
    const std::array<case_spec, 4> specs{{{0, 16}, {20, 16}, {32, 16}, {5000, 1024}}};
    for (const auto kind : all_modes) {
        for (const auto &spec : specs) {
            const auto payload = test::support::make_payload(spec.logical_bytes);
            const auto stream = test::support::build_rscc(payload, kind, spec.block_size);
            REQUIRE(rscc_logical_size(stream) == static_cast<std::uint64_t>(spec.logical_bytes));
        }
    }
}

TEST_CASE("rscc: inspects the declared size when the block cannot be decoded")
{
    // A structurally valid single-block deflate stream whose block demands a preset
    // dictionary: the header and envelope are valid, so size inspection must report the
    // declared logical size even though the block cannot be decoded.
    constexpr std::array<std::byte, 6> needs_dictionary{std::byte{0x78}, std::byte{0xbb}, std::byte{0},
                                                        std::byte{0},    std::byte{0},    std::byte{1}};
    auto container = rscc_header(1, 16, 8);
    append_u32(container, static_cast<std::uint32_t>(needs_dictionary.size()));
    container.insert(container.end(), needs_dictionary.begin(), needs_dictionary.end());
    append_magic(container);

    REQUIRE(rscc_logical_size(container) == 8);
    REQUIRE_THROWS_AS(decompress_rscc(container), rscc_error);
}

TEST_CASE("rscc: a corrupt compressed payload keeps its declared size")
{
    const auto payload = test::support::make_payload(64);
    const auto valid = test::support::build_rscc(payload, compression_kind::zstd, 16);
    REQUIRE(rscc_logical_size(valid) == 64);

    // Corrupt the first byte of the first compressed block, which is the zstd frame
    // magic. The header, table and trailing magic stay intact, so the declared size is
    // still truthfully reported while the block decode fails.
    const std::uint64_t block_count = 64 / 16 + 1;
    const std::size_t data_start = 16 + static_cast<std::size_t>(block_count) * 4;
    auto corrupt = valid;
    corrupt[data_start] ^= std::byte{0xFF};

    REQUIRE(rscc_logical_size(corrupt) == 64);
    REQUIRE_THROWS_AS(decompress_rscc(corrupt), rscc_error);
}

TEST_CASE("rscc: size inspection rejects malformed headers and envelopes")
{
    const auto payload = test::support::make_payload(40);
    const auto valid = test::support::build_rscc(payload, compression_kind::zstd, 16);
    REQUIRE(rscc_logical_size(valid) == 40);

    REQUIRE_THROWS_AS(rscc_logical_size(std::vector<std::byte>(15)), rscc_error);

    auto bad_header = valid;
    bad_header[0] = std::byte{'X'};
    REQUIRE_THROWS_AS(rscc_logical_size(bad_header), rscc_error);

    auto bad_trailer = valid;
    corrupt_last_byte(bad_trailer);
    REQUIRE_THROWS_AS(rscc_logical_size(bad_trailer), rscc_error);

    auto zero_block = rscc_header(2, 0, 16);
    append_magic(zero_block);
    REQUIRE_THROWS_AS(rscc_logical_size(zero_block), rscc_error);

    auto unknown_mode = rscc_header(5, 16, 0);
    append_magic(unknown_mode);
    REQUIRE_THROWS_AS(rscc_logical_size(unknown_mode), rscc_error);

    auto huge_count = rscc_header(2, 1, 100000);
    append_magic(huge_count);
    REQUIRE_THROWS_AS(rscc_logical_size(huge_count), rscc_error);

    auto short_table = rscc_header(2, 16, 64);
    short_table.resize(26);
    const auto magic = rscc_magic();
    std::copy(magic.begin(), magic.end(), short_table.end() - 4);
    REQUIRE_THROWS_AS(rscc_logical_size(short_table), rscc_error);

    rscc_limits tiny_input;
    tiny_input.max_input_bytes = 8;
    REQUIRE_THROWS_AS(rscc_logical_size(valid, tiny_input), rscc_error);

    rscc_limits tiny_output;
    tiny_output.max_output_bytes = 8;
    REQUIRE_THROWS_AS(rscc_logical_size(valid, tiny_output), rscc_error);

    rscc_limits few_blocks;
    few_blocks.max_blocks = 1;
    REQUIRE_THROWS_AS(rscc_logical_size(valid, few_blocks), rscc_error);
}

TEST_CASE("rscc: size inspection honors the output budget boundary")
{
    const auto zero = test::support::build_rscc({}, compression_kind::deflate, 16);
    REQUIRE(rscc_logical_size(zero) == 0);
    REQUIRE(decompress_rscc(zero).empty());

    const auto payload = test::support::make_payload(34);
    const auto stream = test::support::build_rscc(payload, compression_kind::deflate, 16);

    rscc_limits exact;
    exact.max_output_bytes = 34;
    REQUIRE(rscc_logical_size(stream, exact) == 34);
    REQUIRE(decompress_rscc(stream, exact) == payload);

    rscc_limits one_over;
    one_over.max_output_bytes = 33;
    REQUIRE_THROWS_AS(rscc_logical_size(stream, one_over), rscc_error);
    REQUIRE_THROWS_AS(decompress_rscc(stream, one_over), rscc_error);
}

#ifdef _DEBUG
namespace
{
    // Records the byte count of the first allocation a wrapped C library requests
    // through its public allocator hook. The CRT fault-injection hook then targets
    // exactly that request instead of knocking out the first unrelated allocation
    // (such as the MSVC debug `std::vector` container proxy), which would abort the
    // noexcept `std::vector` constructor instead of failing inside the library.
    struct allocation_probe final
    {
        std::size_t first_request = 0;
        std::size_t allocation_count = 0;
    };

    void *__cdecl zlib_probe_alloc(void *opaque, const unsigned int items, const unsigned int size)
    {
        auto *probe = static_cast<allocation_probe *>(opaque);
        if (probe->allocation_count == 0) {
            probe->first_request = static_cast<std::size_t>(items) * static_cast<std::size_t>(size);
        }
        probe->allocation_count += 1;
        return std::calloc(items, size);
    }

    void __cdecl zlib_probe_free(void *, void *address)
    {
        std::free(address);
    }

    // Runs one real `inflateInit2` against the test allocator to learn the size of the
    // first library allocation, then tears the stream down. No CRT hook is armed yet.
    allocation_probe calibrate_zlib_first_allocation(const int window_bits)
    {
        allocation_probe probe;
        z_stream stream{};
        stream.zalloc = zlib_probe_alloc;
        stream.zfree = zlib_probe_free;
        stream.opaque = &probe;
        if (inflateInit2(&stream, window_bits) == Z_OK) {
            static_cast<void>(inflateEnd(&stream));
        }
        return probe;
    }

    void *__cdecl brotli_probe_alloc(void *opaque, const std::size_t size)
    {
        auto *probe = static_cast<allocation_probe *>(opaque);
        if (probe->allocation_count == 0) {
            probe->first_request = size;
        }
        probe->allocation_count += 1;
        return std::calloc(1, size);
    }

    void __cdecl brotli_probe_free(void *, void *address)
    {
        std::free(address);
    }

    allocation_probe calibrate_brotli_first_allocation()
    {
        allocation_probe probe;
        BrotliDecoderState *state = BrotliDecoderCreateInstance(brotli_probe_alloc, brotli_probe_free, &probe);
        if (state != nullptr) {
            BrotliDecoderDestroyInstance(state);
        }
        return probe;
    }

    // Scalar-only state shared with the CRT hook. The hook must never allocate, log or
    // throw, so it records a rejected count and a fired flag and nothing else.
    struct hook_state final
    {
        std::size_t target_size = 0;
        bool armed = false;
        bool fired = false;
        std::size_t rejected = 0;
    };

    // Only the arming thread consults the hook, so unrelated threads keep their normal
    // allocation behaviour.
    thread_local hook_state *active_hook = nullptr;

    // Rejects only one exact-size initial ALLOC from the target library, never an ALLOC of
    // another size nor any REALLOC or other event. Frees, CRT-internal blocks and every
    // other size pass through untouched, so the C++ container proxy and Catch2 bookkeeping
    // keep working while the library call is being failed.
    int __cdecl reject_target_allocation_hook(const int allocation_type, void *, const std::size_t size,
                                              const int block_type, const long, const unsigned char *, const int)
    {
        hook_state *state = active_hook;
        if (state == nullptr || !state->armed) {
            return 1;
        }
        if (allocation_type != _HOOK_ALLOC) {
            return 1;
        }
        if (block_type == _CRT_BLOCK) {
            return 1;
        }
        if (size != state->target_size) {
            return 1;
        }
        state->armed = false;
        state->fired = true;
        state->rejected += 1;
        return 0;
    }

    // Points the current thread at one hook state for the lifetime of the scope.
    class scoped_active_hook final
    {
      public:
        explicit scoped_active_hook(hook_state &state) noexcept
        {
            active_hook = &state;
        }

        ~scoped_active_hook()
        {
            active_hook = nullptr;
        }

        scoped_active_hook(const scoped_active_hook &) = delete;
        scoped_active_hook &operator=(const scoped_active_hook &) = delete;
    };

    // Arms the CRT hook with one target size and restores the previous hook before the
    // enclosing Catch2 assertion runs.
    class scoped_target_allocation_failure final
    {
      public:
        explicit scoped_target_allocation_failure(const std::size_t target_size)
            : previous_(_CrtSetAllocHook(reject_target_allocation_hook))
        {
            state_.target_size = target_size;
            state_.armed = true;
            active_hook = &state_;
        }

        ~scoped_target_allocation_failure()
        {
            state_.armed = false;
            active_hook = nullptr;
            static_cast<void>(_CrtSetAllocHook(previous_));
        }

        scoped_target_allocation_failure(const scoped_target_allocation_failure &) = delete;
        scoped_target_allocation_failure &operator=(const scoped_target_allocation_failure &) = delete;

        [[nodiscard]] bool fired() const noexcept
        {
            return state_.fired;
        }

      private:
        hook_state state_{};
        _CRT_ALLOC_HOOK previous_ = nullptr;
    };

    struct fault_probe final
    {
        bool reported_compression_error = false;
        bool target_allocation_rejected = false;
    };

    // Fails the calibrated library allocation, then observes that our adapter turns the
    // library's failure into `compression_error`. The guard is destroyed before this
    // returns, so the hook is gone before any Catch2 assertion inspects the result.
    fault_probe decompress_with_failed_initial_allocation(const compression_kind kind, const std::size_t target_size)
    {
        fault_probe result;
        scoped_target_allocation_failure failure(target_size);
        try {
            static_cast<void>(decompress_block({}, kind, 0));
        } catch (const compression_error &) {
            result.reported_compression_error = true;
        }
        result.target_allocation_rejected = failure.fired();
        return result;
    }
    fault_probe streaming_with_failed_initial_allocation(const compression_kind kind, const std::size_t target_size)
    {
        fault_probe result;
        const auto no_source = [](std::uint64_t, std::span<std::byte>) {
            FAIL("initialization must not read a compressed block");
        };
        scoped_target_allocation_failure failure(target_size);
        try {
            block_stream_decoder decoder(kind, 0, 0, no_source);
        } catch (const compression_error &) {
            result.reported_compression_error = true;
        }
        result.target_allocation_rejected = failure.fired();
        return result;
    }

} // namespace

TEST_CASE("resource compression: allocation hook rejects only the calibrated target")
{
    hook_state state;
    state.target_size = 256;
    state.armed = true;
    const scoped_active_hook active(state);

    // While armed, only one exact target allocation will ever be refused. Everything
    // else passes through without firing: an unrelated size, a free, a CRT-internal
    // block and a reallocation that happens to carry the target size.
    REQUIRE(reject_target_allocation_hook(_HOOK_ALLOC, nullptr, 16, _NORMAL_BLOCK, 0, nullptr, 0) == 1);
    REQUIRE(reject_target_allocation_hook(_HOOK_FREE, nullptr, 256, _NORMAL_BLOCK, 0, nullptr, 0) == 1);
    REQUIRE(reject_target_allocation_hook(_HOOK_ALLOC, nullptr, 256, _CRT_BLOCK, 0, nullptr, 0) == 1);
    REQUIRE(reject_target_allocation_hook(_HOOK_REALLOC, nullptr, 256, _NORMAL_BLOCK, 0, nullptr, 0) == 1);
    REQUIRE(!(state.fired));
    REQUIRE(state.rejected == 0);

    // The first exact matching normal allocation is refused and disarms the hook
    // immediately, so allocations made while reporting that error still succeed.
    REQUIRE(reject_target_allocation_hook(_HOOK_ALLOC, nullptr, 256, _NORMAL_BLOCK, 0, nullptr, 0) == 0);
    REQUIRE(state.fired);
    REQUIRE(state.rejected == 1);
    REQUIRE(!(state.armed));

    // Once disarmed, neither another allocation nor a matching reallocation is refused.
    REQUIRE(reject_target_allocation_hook(_HOOK_ALLOC, nullptr, 256, _NORMAL_BLOCK, 0, nullptr, 0) == 1);
    REQUIRE(reject_target_allocation_hook(_HOOK_REALLOC, nullptr, 256, _NORMAL_BLOCK, 0, nullptr, 0) == 1);
    REQUIRE(state.rejected == 1);
}

TEST_CASE("resource compression: initialization failures are normalized")
{
    const allocation_probe deflate_target = calibrate_zlib_first_allocation(15);
    const allocation_probe gzip_target = calibrate_zlib_first_allocation(15 + 16);
    const allocation_probe brotli_target = calibrate_brotli_first_allocation();

    REQUIRE(deflate_target.allocation_count > 0);
    REQUIRE(deflate_target.first_request > 0);
    REQUIRE(gzip_target.allocation_count > 0);
    REQUIRE(gzip_target.first_request > 0);
    REQUIRE(brotli_target.allocation_count > 0);
    REQUIRE(brotli_target.first_request > 0);

    // The calibrated library targets must not collide with the small MSVC container
    // proxy allocation (observed at sizeof(void *) * 2), which the hook lets through.
    REQUIRE(deflate_target.first_request != sizeof(void *) * 2);
    REQUIRE(gzip_target.first_request != sizeof(void *) * 2);
    REQUIRE(brotli_target.first_request != sizeof(void *) * 2);

    const fault_probe deflate =
        decompress_with_failed_initial_allocation(compression_kind::deflate, deflate_target.first_request);
    REQUIRE(deflate.reported_compression_error);
    REQUIRE(deflate.target_allocation_rejected);

    const fault_probe gzip =
        decompress_with_failed_initial_allocation(compression_kind::gzip, gzip_target.first_request);
    REQUIRE(gzip.reported_compression_error);
    REQUIRE(gzip.target_allocation_rejected);

    const fault_probe brotli =
        decompress_with_failed_initial_allocation(compression_kind::brotli, brotli_target.first_request);
    REQUIRE(brotli.reported_compression_error);
    REQUIRE(brotli.target_allocation_rejected);

    const fault_probe streaming_deflate =
        streaming_with_failed_initial_allocation(compression_kind::deflate, deflate_target.first_request);
    REQUIRE(streaming_deflate.reported_compression_error);
    REQUIRE(streaming_deflate.target_allocation_rejected);
    const fault_probe streaming_gzip =
        streaming_with_failed_initial_allocation(compression_kind::gzip, gzip_target.first_request);
    REQUIRE(streaming_gzip.reported_compression_error);
    REQUIRE(streaming_gzip.target_allocation_rejected);
    const fault_probe streaming_brotli =
        streaming_with_failed_initial_allocation(compression_kind::brotli, brotli_target.first_request);
    REQUIRE(streaming_brotli.reported_compression_error);
    REQUIRE(streaming_brotli.target_allocation_rejected);
}

TEST_CASE("rscc: streaming codec initialization failure is reported as an RSCC error",
          "[godot][rscc-metadata][allocation]")
{
    const auto target = calibrate_zlib_first_allocation(15);
    REQUIRE(target.first_request > 0);
    const auto payload = test::support::make_payload(40);
    const auto stored = test::support::build_rscc(payload, compression_kind::deflate, 16);
    const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> output) {
        REQUIRE(offset <= stored.size());
        REQUIRE(output.size() <= stored.size() - offset);
        std::memcpy(output.data(), stored.data() + offset, output.size());
    };
    rscc_metadata_reader reader(stored.size(), source);
    std::array<std::byte, 1> output{};
    bool translated = false;
    bool failed_target = false;
    {
        scoped_target_allocation_failure fail(target.first_request);
        try {
            reader.read(0, output);
        } catch (const rscc_error &) {
            translated = true;
        }
        failed_target = fail.fired();
    }
    CHECK(failed_target);
    CHECK(translated);
}

namespace
{
    struct zstd_allocation_probe final
    {
        std::array<std::size_t, 16> sizes{};
        std::size_t count = 0;
    };

    void *zstd_probe_alloc(void *opaque, const std::size_t size)
    {
        auto &probe = *static_cast<zstd_allocation_probe *>(opaque);
        if (probe.count < probe.sizes.size())
            probe.sizes[probe.count] = size;
        ++probe.count;
        return std::malloc(size);
    }

    void zstd_probe_free(void *, void *address)
    {
        std::free(address);
    }
} // namespace

TEST_CASE("resource compression: ZSTD streaming context allocation phases are observable",
          "[godot][block-stream][zstd-allocation]")
{
    zstd_allocation_probe probe;
    const ZSTD_customMem allocator{zstd_probe_alloc, zstd_probe_free, &probe};
    ZSTD_DStream *stream = ZSTD_createDStream_advanced(allocator);
    REQUIRE(stream != nullptr);
    const std::size_t create_count = probe.count;
    const std::size_t status = ZSTD_initDStream(stream);
    const std::size_t init_count = probe.count;
    static_cast<void>(ZSTD_freeDStream(stream));
    REQUIRE(!ZSTD_isError(status));
    REQUIRE(create_count > 0);
    CAPTURE(create_count, init_count, probe.sizes);
    CHECK(init_count == create_count);
    const fault_probe rejected = streaming_with_failed_initial_allocation(compression_kind::zstd, probe.sizes[0]);
    CHECK(rejected.target_allocation_rejected);
    CHECK(rejected.reported_compression_error);
}
#endif

TEST_CASE("rscc: a block table changed after metadata validation cannot escape its envelope",
          "[godot][rscc-metadata][changed-table]")
{
    const auto payload = test::support::make_payload(40);
    auto stored = test::support::build_rscc(payload, compression_kind::deflate, 16);
    const auto source = [&stored](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= stored.size());
        REQUIRE(target.size() <= stored.size() - offset);
        std::memcpy(target.data(), stored.data() + offset, target.size());
    };
    rscc_metadata_reader reader(stored.size(), source);
    REQUIRE(reader.logical_size() == payload.size());
    stored[16] = stored[17] = stored[18] = stored[19] = std::byte{0xFF};
    std::array<std::byte, 1> byte{};
    CHECK_THROWS_AS(reader.read(0, byte), rscc_error);
}

TEST_CASE("resource compression: streaming FastLZ accepts level-one escaped length and rejects bad terminal envelopes",
          "[godot][block-stream][fastlz-terminal]")
{
    auto escaped = byte_stream({0x06});
    escaped = repeat(std::move(escaped), 7, 0x41);
    escaped.push_back(std::byte{0xE0});
    escaped.push_back(std::byte{0x00});
    escaped.push_back(std::byte{0x00});
    const auto source = [&escaped](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= escaped.size());
        REQUIRE(target.size() <= escaped.size() - offset);
        std::memcpy(target.data(), escaped.data() + offset, target.size());
    };
    block_stream_decoder valid(compression_kind::fastlz, escaped.size(), 16, source);
    std::array<std::byte, 16> output{};
    valid.read(0, output);
    CHECK(output == std::array<std::byte, 16>{std::byte{0x41}, std::byte{0x41}, std::byte{0x41}, std::byte{0x41},
                                              std::byte{0x41}, std::byte{0x41}, std::byte{0x41}, std::byte{0x41},
                                              std::byte{0x41}, std::byte{0x41}, std::byte{0x41}, std::byte{0x41},
                                              std::byte{0x41}, std::byte{0x41}, std::byte{0x41}, std::byte{0x41}});
    CHECK_THROWS_AS(valid.read(17, {}), compression_error);

    block_stream_decoder wrong_length(compression_kind::fastlz, escaped.size(), 17, source);
    CHECK_THROWS_AS(wrong_length.finish(), compression_error);
    auto trailing = escaped;
    trailing.push_back(std::byte{0x7F});
    const auto trailing_source = [&trailing](const std::uint64_t offset, const std::span<std::byte> target) {
        REQUIRE(offset <= trailing.size());
        REQUIRE(target.size() <= trailing.size() - offset);
        std::memcpy(target.data(), trailing.data() + offset, target.size());
    };
    block_stream_decoder wrong_stored(compression_kind::fastlz, trailing.size(), 16, trailing_source);
    CHECK_THROWS_AS(wrong_stored.finish(), compression_error);
}
