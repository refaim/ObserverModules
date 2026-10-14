#include "../../core/codecs/bc_codec.h"
#include "../../core/codecs/png_codec.h"
#include "../support/archive_lazy_fixtures.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <span>
#include <string>
#include <vector>

#ifdef _DEBUG
#include <crtdbg.h>
#endif
#include <windows.h>

// The tests decode PNG streams with libspng directly so that the encoder is judged against an
// independent reader rather than its own state. libspng is a static port, so its declarations must
// not carry the dllimport decoration; the macro stays private to this translation unit.
#define SPNG_STATIC
#include <spng.h>

#include <catch2/catch_test_macros.hpp>

namespace
{
    std::vector<std::byte> bytes(const std::initializer_list<unsigned int> values)
    {
        std::vector<std::byte> result;
        result.reserve(values.size());
        for (const unsigned int value : values) {
            result.push_back(static_cast<std::byte>(value));
        }
        return result;
    }

    std::array<std::byte, 4> pixel_at(const unpackrat::codecs::rgba_image &image, const std::uint32_t x,
                                      const std::uint32_t y)
    {
        const auto offset = (static_cast<std::size_t>(y) * image.width + x) * 4;
        REQUIRE(offset + 4 <= image.pixels.size());
        return {image.pixels[offset], image.pixels[offset + 1], image.pixels[offset + 2], image.pixels[offset + 3]};
    }

    struct decoded_png final
    {
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::vector<std::byte> pixels;
    };

    // Decodes a PNG stream into RGBA8 with libspng. This is the independent reader the encoder is
    // checked against; the library usage is test-only and does not exercise production code.
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

    unpackrat::codecs::rgba_image make_image(const std::uint32_t width, const std::uint32_t height,
                                             const std::vector<std::byte> &pixels)
    {
        unpackrat::codecs::rgba_image image;
        image.width = width;
        image.height = height;
        image.pixels = pixels;
        return image;
    }

#ifdef _DEBUG
    // Drives the debug CRT allocation hook so the encoder's context-allocation and internal-encode
    // failure paths can be reached without a real out-of-memory condition. reject_index is the
    // one-based index of the single allocation to fail; zero disables the policy. Failing exactly
    // one allocation mirrors the existing zlib initialization-failure test and leaves the error
    // reporting that follows free to allocate normally.
    thread_local std::size_t allocation_count = 0;
    thread_local std::size_t reject_index = 0;
    thread_local bool calibrate_error_allocation = false;
    thread_local std::size_t error_allocation_size = 0;
    thread_local std::size_t reject_size = 0;
    thread_local bool target_rejected = false;
#ifdef _WIN64
    thread_local bool reject_only_in_spng = false;
    thread_local DWORD64 encoder_begin = 0;
    thread_local DWORD64 encoder_end = 0;
    bool encoder_is_on_stack() noexcept
    {
        std::array<void *, 32> stack{};
        const auto count = CaptureStackBackTrace(0, static_cast<DWORD>(stack.size()), stack.data(), nullptr);
        for (USHORT index = 0; index < count; ++index) {
            const auto address = reinterpret_cast<DWORD64>(stack[index]);
            if (address >= encoder_begin && address < encoder_end)
                return true;
        }
        return false;
    }
#endif

    int __cdecl allocation_hook(const int allocation_type, void *, const std::size_t size, const int block_type,
                                const long, const unsigned char *, const int)
    {
        if (allocation_type == _HOOK_ALLOC) {
            ++allocation_count;
            if (calibrate_error_allocation && error_allocation_size == 0 && block_type == _NORMAL_BLOCK)
                error_allocation_size = size;
            if (reject_size != 0 && size == reject_size && block_type == _NORMAL_BLOCK
#ifdef _WIN64
                && (!reject_only_in_spng || encoder_is_on_stack())
#endif
            ) {
                reject_size = 0;
                target_rejected = true;
                return 0;
            }
            if (reject_index != 0 && allocation_count == reject_index) {
                return 0;
            }
        }
        return 1;
    }

    class scoped_allocation_policy final
    {
      public:
        explicit scoped_allocation_policy(const std::size_t index) : previous_(_CrtSetAllocHook(allocation_hook))
        {
            allocation_count = 0;
            reject_index = index;
            calibrate_error_allocation = false;
            error_allocation_size = 0;
            reject_size = 0;
            target_rejected = false;
#ifdef _WIN64
            reject_only_in_spng = false;
#endif
        }

        ~scoped_allocation_policy()
        {
            reject_index = 0;
            reject_size = 0;
            calibrate_error_allocation = false;
#ifdef _WIN64
            reject_only_in_spng = false;
#endif
            static_cast<void>(_CrtSetAllocHook(previous_));
        }

        scoped_allocation_policy(const scoped_allocation_policy &) = delete;
        scoped_allocation_policy &operator=(const scoped_allocation_policy &) = delete;

      private:
        _CRT_ALLOC_HOOK previous_ = nullptr;
    };

    enum class png_outcome
    {
        none,
        image_error,
        other,
    };

    png_outcome encode_with_allocation_policy(const unpackrat::codecs::rgba_image &image,
                                              const std::function<void(std::span<const std::byte>)> &sink,
                                              const std::size_t reject)
    {
        png_outcome outcome = png_outcome::none;
        scoped_allocation_policy policy(reject);
        try {
            unpackrat::codecs::encode_png(image, sink);
        } catch (const unpackrat::codecs::image_error &) {
            outcome = png_outcome::image_error;
        } catch (...) {
            outcome = png_outcome::other;
        }
        return outcome;
    }
#endif
} // namespace

TEST_CASE("png codec: encodes an RGBA image that an independent decoder reads back exactly")
{
    const auto pixels = bytes({
        10,  20,  30,  40,  50,  60,  70,  80,  90,  100, 110, 120, // row 0
        130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230, 240, // row 1
    });
    const auto image = make_image(3, 2, pixels);

    std::vector<std::byte> stream;
    std::size_t sink_calls = 0;
    unpackrat::codecs::encode_png(image, [&](const std::span<const std::byte> chunk) {
        ++sink_calls;
        stream.insert(stream.end(), chunk.begin(), chunk.end());
    });

    REQUIRE(sink_calls >= 1);
    const std::array<std::byte, 8> signature{
        std::byte{0x89}, std::byte{0x50}, std::byte{0x4E}, std::byte{0x47},
        std::byte{0x0D}, std::byte{0x0A}, std::byte{0x1A}, std::byte{0x0A},
    };
    REQUIRE(stream.size() > signature.size());
    REQUIRE(std::equal(signature.begin(), signature.end(), stream.begin()));

    const auto decoded = decode_png(stream);
    REQUIRE(decoded.width == 3);
    REQUIRE(decoded.height == 2);
    REQUIRE(decoded.pixels.size() == pixels.size());
    REQUIRE(decoded.pixels == pixels);
}

TEST_CASE("png codec: releases its context across repeated encodes")
{
    for (std::uint32_t side = 1; side <= 8; ++side) {
        std::vector<std::byte> pixels(static_cast<std::size_t>(side) * side * 4);
        for (std::size_t index = 0; index < pixels.size(); ++index) {
            pixels[index] = static_cast<std::byte>(index * 7 + side);
        }
        const auto image = make_image(side, side, pixels);

        std::vector<std::byte> stream;
        unpackrat::codecs::encode_png(image, [&](const std::span<const std::byte> chunk) {
            stream.insert(stream.end(), chunk.begin(), chunk.end());
        });

        const auto decoded = decode_png(stream);
        REQUIRE(decoded.pixels == pixels);
    }
}

TEST_CASE("png codec: rejects malformed descriptions and breached limits")
{
    SECTION("zero width")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::encode_png(make_image(0, 1, {}), [](std::span<const std::byte>) {}),
                          unpackrat::codecs::image_error);
    }
    SECTION("zero height")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::encode_png(make_image(1, 0, {}), [](std::span<const std::byte>) {}),
                          unpackrat::codecs::image_error);
    }
    SECTION("pixel buffer too small for the dimensions")
    {
        REQUIRE_THROWS_AS(
            unpackrat::codecs::encode_png(make_image(2, 2, bytes({1, 2, 3, 4})), [](std::span<const std::byte>) {}),
            unpackrat::codecs::image_error);
    }
    SECTION("pixel buffer larger than the dimensions")
    {
        REQUIRE_THROWS_AS(
            unpackrat::codecs::encode_png(make_image(1, 1, bytes({1, 2, 3, 4, 5})), [](std::span<const std::byte>) {}),
            unpackrat::codecs::image_error);
    }
    SECTION("pixel budget exceeded")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_pixels = 15;
        REQUIRE_THROWS_AS(unpackrat::codecs::encode_png(
                              make_image(4, 4, std::vector<std::byte>(64)), [](std::span<const std::byte>) {}, limits),
                          unpackrat::codecs::image_error);
    }
    SECTION("dimensions overflow the byte count")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_pixels = (std::numeric_limits<std::uint64_t>::max)();
        REQUIRE_THROWS_AS(unpackrat::codecs::encode_png(
                              make_image(0xFFFFFFFFu, 0xFFFFFFFFu, {}), [](std::span<const std::byte>) {}, limits),
                          unpackrat::codecs::image_error);
    }
}

TEST_CASE("png codec: enforces the output byte budget before publishing anything")
{
    std::vector<std::byte> published;
    unpackrat::codecs::image_limits limits;
    limits.max_output_bytes = 0;

    REQUIRE_THROWS_AS(unpackrat::codecs::encode_png(
                          make_image(1, 1, bytes({9, 8, 7, 6})),
                          [&](const std::span<const std::byte> chunk) {
                              published.insert(published.end(), chunk.begin(), chunk.end());
                          },
                          limits),
                      unpackrat::codecs::image_error);
    REQUIRE(published.empty());
}

TEST_CASE("png codec: reports a sink byte budget breach as an image error")
{
    std::size_t sink_calls = 0;
    unpackrat::codecs::image_limits limits;
    limits.max_output_bytes = 8; // lets the signature through, fails the first real chunk

    REQUIRE_THROWS_AS(
        unpackrat::codecs::encode_png(
            make_image(1, 1, bytes({1, 2, 3, 4})), [&](std::span<const std::byte>) { ++sink_calls; }, limits),
        unpackrat::codecs::image_error);
    REQUIRE(sink_calls >= 1);
}

TEST_CASE("png codec: propagates the sink's own exception unchanged")
{
    class sink_failure final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    bool caught = false;
    try {
        unpackrat::codecs::encode_png(make_image(1, 1, bytes({1, 2, 3, 4})), [](std::span<const std::byte>) {
            throw sink_failure("sink rejected the chunk");
        });
    } catch (const sink_failure &error) {
        caught = true;
        REQUIRE(std::string(error.what()) == "sink rejected the chunk");
    }
    REQUIRE(caught);
}

TEST_CASE("png codec: an image that is one pixel within budget still encodes")
{
    unpackrat::codecs::image_limits limits;
    limits.max_pixels = 1;
    limits.max_output_bytes = 4096;

    std::vector<std::byte> stream;
    unpackrat::codecs::encode_png(
        make_image(1, 1, bytes({1, 2, 3, 4})),
        [&](const std::span<const std::byte> chunk) { stream.insert(stream.end(), chunk.begin(), chunk.end()); },
        limits);
    const auto decoded = decode_png(stream);
    REQUIRE(decoded.pixels == bytes({1, 2, 3, 4}));
}

TEST_CASE("bc codec: decodes a known BC1 block with binary alpha")
{
    const auto input = bytes({0xFF, 0xFF, 0x00, 0x00, 0xE4, 0x00, 0x00, 0x00});
    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc1, 4, 4);

    REQUIRE(image.width == 4);
    REQUIRE(image.height == 4);
    REQUIRE(image.pixels.size() == 64);
    REQUIRE(pixel_at(image, 0, 0) == std::array{std::byte{255}, std::byte{255}, std::byte{255}, std::byte{255}});
    REQUIRE(pixel_at(image, 1, 0) == std::array{std::byte{0}, std::byte{0}, std::byte{0}, std::byte{255}});
    REQUIRE(pixel_at(image, 2, 0) == std::array{std::byte{170}, std::byte{170}, std::byte{170}, std::byte{255}});
    REQUIRE(pixel_at(image, 3, 0) == std::array{std::byte{85}, std::byte{85}, std::byte{85}, std::byte{255}});
    REQUIRE(pixel_at(image, 0, 1) == std::array{std::byte{255}, std::byte{255}, std::byte{255}, std::byte{255}});
    REQUIRE(pixel_at(image, 3, 3) == std::array{std::byte{255}, std::byte{255}, std::byte{255}, std::byte{255}});
}

TEST_CASE("bc codec: decodes a known BC2 block with explicit alpha")
{
    const auto input =
        bytes({0x0F, 0xF0, 0xAA, 0x55, 0x00, 0xFF, 0x33, 0xCC, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc2, 4, 4);

    REQUIRE(image.pixels.size() == 64);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 0, 0)[3]) == 255);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 1, 0)[3]) == 0);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 0, 1)[3]) == 170);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 1, 1)[3]) == 170);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 2, 1)[3]) == 85);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 1, 3)[3]) == 51);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 2, 3)[3]) == 204);
    REQUIRE(pixel_at(image, 0, 0) == std::array{std::byte{255}, std::byte{255}, std::byte{255}, std::byte{255}});
}

TEST_CASE("bc codec: decodes a known BC3 block with interpolated alpha")
{
    const auto input =
        bytes({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc3, 4, 4);

    REQUIRE(image.pixels.size() == 64);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 0, 0)[3]) == 0);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 1, 0)[3]) == 255);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 2, 0)[3]) == 51);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 3, 0)[3]) == 102);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 0, 1)[3]) == 153);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 1, 1)[3]) == 204);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 2, 1)[3]) == 0);
    REQUIRE(static_cast<unsigned>(pixel_at(image, 3, 1)[3]) == 255);
}

TEST_CASE("bc codec: expands BC4 single-channel data to (R, 0, 0, 255)")
{
    const auto input = bytes({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00});
    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc4, 4, 4);

    REQUIRE(image.pixels.size() == 64);
    const std::array<unsigned, 16> red{0, 255, 51, 102, 153, 204, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0};
    for (std::size_t index = 0; index < red.size(); ++index) {
        const auto pixel = image.pixels[index * 4];
        REQUIRE(static_cast<unsigned>(pixel) == red[index]);
        REQUIRE(image.pixels[index * 4 + 1] == std::byte{0});
        REQUIRE(image.pixels[index * 4 + 2] == std::byte{0});
        REQUIRE(image.pixels[index * 4 + 3] == std::byte{255});
    }
}

TEST_CASE("bc codec: expands BC5 two-channel data to (R, G, 0, 255)")
{
    const auto input =
        bytes({0x00, 0xFF, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00, 0xFF, 0x00, 0x88, 0xC6, 0xFA, 0x00, 0x00, 0x00});
    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc5, 4, 4);

    REQUIRE(image.pixels.size() == 64);
    const std::array<unsigned, 16> red{0, 255, 51, 102, 153, 204, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0};
    const std::array<unsigned, 16> green{255, 0, 218, 182, 145, 109, 72, 36, 255, 255, 255, 255, 255, 255, 255, 255};
    for (std::size_t index = 0; index < red.size(); ++index) {
        REQUIRE(static_cast<unsigned>(image.pixels[index * 4]) == red[index]);
        REQUIRE(static_cast<unsigned>(image.pixels[index * 4 + 1]) == green[index]);
        REQUIRE(image.pixels[index * 4 + 2] == std::byte{0});
        REQUIRE(image.pixels[index * 4 + 3] == std::byte{255});
    }
}

TEST_CASE("bc codec: decodes a known BC7 block")
{
    const auto input =
        bytes({0x40, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF});
    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc7, 4, 4);

    REQUIRE(image.pixels.size() == 64);
    REQUIRE(pixel_at(image, 0, 0) == std::array{std::byte{254}, std::byte{255}, std::byte{255}, std::byte{255}});
    REQUIRE(pixel_at(image, 1, 0) == std::array{std::byte{255}, std::byte{255}, std::byte{255}, std::byte{255}});
    REQUIRE(pixel_at(image, 3, 3) == std::array{std::byte{255}, std::byte{255}, std::byte{255}, std::byte{255}});
}

TEST_CASE("bc codec: crops partial edge blocks without reading past the payload")
{
    // 6x5 image over 2x2 BC1 blocks: red/green on the top row, blue/white on the bottom row.
    const auto red = bytes({0x00, 0xF8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto green = bytes({0xE0, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto blue = bytes({0x1F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    const auto white = bytes({0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00});
    std::vector<std::byte> input;
    for (const auto *block : {&red, &green, &blue, &white}) {
        input.insert(input.end(), block->begin(), block->end());
    }

    const auto image = unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc1, 6, 5);
    REQUIRE(image.width == 6);
    REQUIRE(image.height == 5);
    REQUIRE(image.pixels.size() == std::size_t{6} * 5 * 4);

    const auto is_color = [](const std::array<std::byte, 4> pixel, const unsigned red_value, const unsigned green_value,
                             const unsigned blue_value) {
        return static_cast<unsigned>(pixel[0]) == red_value && static_cast<unsigned>(pixel[1]) == green_value &&
               static_cast<unsigned>(pixel[2]) == blue_value && static_cast<unsigned>(pixel[3]) == 255;
    };

    REQUIRE(is_color(pixel_at(image, 0, 0), 255, 0, 0));
    REQUIRE(is_color(pixel_at(image, 3, 0), 255, 0, 0));
    REQUIRE(is_color(pixel_at(image, 4, 0), 0, 255, 0));
    REQUIRE(is_color(pixel_at(image, 5, 0), 0, 255, 0));
    REQUIRE(is_color(pixel_at(image, 0, 3), 255, 0, 0));
    REQUIRE(is_color(pixel_at(image, 0, 4), 0, 0, 255));
    REQUIRE(is_color(pixel_at(image, 3, 4), 0, 0, 255));
    REQUIRE(is_color(pixel_at(image, 4, 4), 255, 255, 255));
    REQUIRE(is_color(pixel_at(image, 5, 4), 255, 255, 255));
}

TEST_CASE("bc codec: decodes a misaligned payload identically to an aligned one")
{
    const auto block = bytes({0xFF, 0xFF, 0x00, 0x00, 0xE4, 0x00, 0x00, 0x00});
    const auto aligned = unpackrat::codecs::decode_bc(block, unpackrat::codecs::bc_format::bc1, 4, 4);

    for (const std::size_t offset : {std::size_t{1}, std::size_t{2}, std::size_t{3}, std::size_t{7}}) {
        std::vector<std::byte> buffer(offset + block.size(), std::byte{0xAB});
        std::memcpy(buffer.data() + offset, block.data(), block.size());
        const auto misaligned = unpackrat::codecs::decode_bc(std::span<const std::byte>(buffer).subspan(offset),
                                                             unpackrat::codecs::bc_format::bc1, 4, 4);
        REQUIRE(misaligned.width == 4);
        REQUIRE(misaligned.height == 4);
        REQUIRE(misaligned.pixels == aligned.pixels);
    }
}

TEST_CASE("bc codec: rejects malformed payloads and unsupported families")
{
    const auto block = std::vector<std::byte>(8, std::byte{0});

    SECTION("zero width")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(block, unpackrat::codecs::bc_format::bc1, 0, 4),
                          unpackrat::codecs::image_error);
    }
    SECTION("zero height")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(block, unpackrat::codecs::bc_format::bc1, 4, 0),
                          unpackrat::codecs::image_error);
    }
    SECTION("unknown block family")
    {
        // Inject a representable but unsupported enum value to verify rejection.
        // NOLINTNEXTLINE(clang-analyzer-optin.core.EnumCastOutOfRange)
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(block, static_cast<unpackrat::codecs::bc_format>(99), 4, 4),
                          unpackrat::codecs::image_error);
    }
    SECTION("payload shorter than the block layout")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(std::vector<std::byte>(7, std::byte{0}),
                                                       unpackrat::codecs::bc_format::bc1, 4, 4),
                          unpackrat::codecs::image_error);
    }
    SECTION("payload longer than the block layout")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(std::vector<std::byte>(9, std::byte{0}),
                                                       unpackrat::codecs::bc_format::bc1, 4, 4),
                          unpackrat::codecs::image_error);
    }
    SECTION("empty payload")
    {
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc({}, unpackrat::codecs::bc_format::bc1, 4, 4),
                          unpackrat::codecs::image_error);
    }
    SECTION("pixel budget exceeded")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_pixels = 15;
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(block, unpackrat::codecs::bc_format::bc1, 4, 4, limits),
                          unpackrat::codecs::image_error);
    }
    SECTION("output budget exceeded")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_output_bytes = 63;
        REQUIRE_THROWS_AS(unpackrat::codecs::decode_bc(block, unpackrat::codecs::bc_format::bc1, 4, 4, limits),
                          unpackrat::codecs::image_error);
    }
    SECTION("input size overflows a 64-bit length")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_pixels = (std::numeric_limits<std::uint64_t>::max)();
        REQUIRE_THROWS_AS(
            unpackrat::codecs::decode_bc({}, unpackrat::codecs::bc_format::bc2, 0xFFFFFFFFu, 0xFFFFFFFFu, limits),
            unpackrat::codecs::image_error);
    }
    SECTION("output byte count overflows a 64-bit length")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_pixels = (std::numeric_limits<std::uint64_t>::max)();
        REQUIRE_THROWS_AS(
            unpackrat::codecs::decode_bc({}, unpackrat::codecs::bc_format::bc1, 0xFFFFFFFFu, 0xFFFFFFFFu, limits),
            unpackrat::codecs::image_error);
    }
}

#ifndef _WIN64
TEST_CASE("bc codec: an unaddressable x86 output is rejected before inspecting its payload", "[bc-x86-bounds]")
{
    // A complete 512 MiB BC1 block grid would expand to 4 GiB of RGBA. Reserve the
    // real input address range without committing or touching any compressed pages.
    constexpr std::size_t input_bytes = std::size_t{512} * 1024 * 1024;
    const HANDLE mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE | SEC_RESERVE, 0,
                                              static_cast<DWORD>(input_bytes), nullptr);
    REQUIRE(mapping != nullptr);
    if (mapping == nullptr) {
        return;
    }
    const auto close_mapping = std::unique_ptr<void, decltype(&CloseHandle)>(mapping, CloseHandle);
    void *const view = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, input_bytes);
    REQUIRE(view != nullptr);
    if (view == nullptr) {
        return;
    }
    const auto unmap_view = std::unique_ptr<void, decltype(&UnmapViewOfFile)>(view, UnmapViewOfFile);
    unpackrat::codecs::image_limits limits;
    std::string diagnostic;
    try {
        const auto input = std::span<const std::byte>(static_cast<const std::byte *>(view), input_bytes);
        static_cast<void>(
            unpackrat::codecs::decode_bc(input, unpackrat::codecs::bc_format::bc1, 1U << 16U, 1U << 14U, limits));
    } catch (const unpackrat::codecs::image_error &error) {
        diagnostic = error.what();
    }
    CHECK(diagnostic == "BC output buffer is not addressable");
}
#endif

#ifdef _DEBUG
#ifdef _WIN64
TEST_CASE("png codec: budget failure never allocates an exception inside the C callback", "[png-callback-oom]")
{
    class scoped_terminate_exit final
    {
      public:
        scoped_terminate_exit() : previous_(std::set_terminate([]() noexcept { std::_Exit(86); }))
        {
        }
        ~scoped_terminate_exit()
        {
            std::set_terminate(previous_);
        }
        scoped_terminate_exit(const scoped_terminate_exit &) = delete;
        scoped_terminate_exit &operator=(const scoped_terminate_exit &) = delete;

      private:
        std::terminate_handler previous_;
    } terminate_policy;
    const auto image = make_image(1, 1, bytes({1, 2, 3, 4}));
    unpackrat::codecs::image_limits limits;
    limits.max_output_bytes = 8;
    std::size_t error_request = 0;
    {
        scoped_allocation_policy calibration(0);
        calibrate_error_allocation = true;
        const unpackrat::codecs::image_error error("PNG output exceeds the configured byte budget");
        static_cast<void>(error.what());
        calibrate_error_allocation = false;
        error_request = error_allocation_size;
    }
    REQUIRE(error_request > 0);
    DWORD64 image_base = 0;
    const auto *code = reinterpret_cast<const unsigned char *>(&spng_encode_image);
    // MSVC incremental Debug linking exposes a five-byte ILT jump without its own unwind entry.
    if (code[0] == 0xe9) {
        std::int32_t displacement = 0;
        std::memcpy(&displacement, code + 1, sizeof(displacement));
        code += 5 + displacement;
    }
    const auto *function = RtlLookupFunctionEntry(reinterpret_cast<DWORD64>(code), &image_base, nullptr);
    REQUIRE(function != nullptr);
    encoder_begin = image_base + function->BeginAddress;
    encoder_end = image_base + function->EndAddress;
    unsigned published = 0;
    bool image_error_caught = false;
    bool correct_message = false;
    bool rejected = false;
    {
        scoped_allocation_policy allocation_policy(0);
        try {
            unpackrat::codecs::encode_png(
                image,
                [&](const std::span<const std::byte> chunk) {
                    published += static_cast<unsigned>(chunk.size());
                    // The next write breaches the configured limit. Refuse its error object's
                    // allocation once: it must propagate only after libspng returns to C++.
                    reject_only_in_spng = true;
                    reject_size = error_request;
                },
                limits);
        } catch (const unpackrat::codecs::image_error &error) {
            image_error_caught = true;
            correct_message = std::strcmp(error.what(), "PNG output exceeds the configured byte budget") == 0;
        }
        rejected = target_rejected;
    }
    CHECK(published == 8);
    CHECK_FALSE(rejected);
    CHECK(image_error_caught);
    CHECK(correct_message);
}
#endif

TEST_CASE("png codec: normalizes an encoder context allocation failure")
{
    std::size_t sink_calls = 0;
    const auto image = make_image(2, 2, std::vector<std::byte>(16));
    const std::function<void(std::span<const std::byte>)> sink = [&](std::span<const std::byte>) { ++sink_calls; };

    // The very first allocation inside encode_png is the encoder context itself.
    REQUIRE(encode_with_allocation_policy(image, sink, 1) == png_outcome::image_error);
    REQUIRE(sink_calls == 0);
}

TEST_CASE("png codec: normalizes an internal encoder failure")
{
    std::vector<std::byte> published;
    published.reserve(4096);
    const auto image = make_image(2, 2, std::vector<std::byte>(16));
    const std::function<void(std::span<const std::byte>)> sink = [&](const std::span<const std::byte> chunk) {
        published.insert(published.end(), chunk.begin(), chunk.end());
    };

    // Let the context be created, then fail the first allocation the encode makes so libspng
    // reports an internal error rather than the sink or the byte budget doing so.
    REQUIRE(encode_with_allocation_policy(image, sink, 2) == png_outcome::image_error);
}
#endif

TEST_CASE("png codec: progressive rows publish output before all pixels are supplied", "[image-streaming]")
{
    std::uint32_t supplied = 0;
    bool first = true;
    std::vector<std::byte> encoded;
    unpackrat::codecs::encode_png_rows(
        3, 20,
        [&](const std::uint32_t row, const std::span<std::byte> bytes) {
            CHECK(row == supplied++);
            REQUIRE(bytes.size() == 12);
            std::fill(bytes.begin(), bytes.end(), std::byte{42});
        },
        [&](const std::span<const std::byte> bytes) {
            if (first) {
                CHECK(supplied < 20);
                first = false;
            }
            encoded.insert(encoded.end(), bytes.begin(), bytes.end());
        });
    const auto decoded = decode_png(encoded);
    CHECK(decoded.width == 3);
    CHECK(decoded.height == 20);
    CHECK(decoded.pixels == std::vector<std::byte>(240, std::byte{42}));
}

TEST_CASE("png codec: progressive failures preserve errors and validate before pixel reads", "[image-streaming]")
{
    using unpackrat::codecs::encode_png_rows;
    using unpackrat::codecs::image_error;
    std::size_t reads = 0;
    const auto read = [&](std::uint32_t, const std::span<std::byte> bytes) {
        ++reads;
        std::fill(bytes.begin(), bytes.end(), std::byte{42});
    };
    const auto discard = [](std::span<const std::byte>) {};
    SECTION("zero dimensions")
    {
        CHECK_THROWS_AS(encode_png_rows(0, 1, read, discard), image_error);
        CHECK_THROWS_AS(encode_png_rows(1, 0, read, discard), image_error);
        CHECK(reads == 0);
    }
    SECTION("PNG format dimension cannot be represented by IHDR")
    {
        CHECK_THROWS_AS(encode_png_rows(0x80000000U, 1, read, discard), image_error);
        CHECK(reads == 0);
    }
    SECTION("configured pixel limit")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_pixels = 15;
        CHECK_THROWS_AS(encode_png_rows(4, 4, read, discard, limits), image_error);
        CHECK(reads == 0);
    }
    SECTION("configured output limit fails during progressive initialization")
    {
        unpackrat::codecs::image_limits limits;
        limits.max_output_bytes = 0;
        CHECK_THROWS_AS(encode_png_rows(4, 4, read, discard, limits), image_error);
        CHECK(reads == 0);
    }
    SECTION("source exception propagates unchanged")
    {
        struct source_failure final : std::runtime_error
        {
            using std::runtime_error::runtime_error;
        };
        CHECK_THROWS_AS(encode_png_rows(
                            4, 4, [](std::uint32_t, std::span<std::byte>) { throw source_failure("source"); }, discard),
                        source_failure);
    }
    SECTION("sink exception during encoded rows propagates unchanged")
    {
        struct sink_failure final : std::runtime_error
        {
            using std::runtime_error::runtime_error;
        };
        CHECK_THROWS_AS(encode_png_rows(4, 4, read,
                                        [&](std::span<const std::byte>) {
                                            if (reads != 0)
                                                throw sink_failure("sink");
                                        }),
                        sink_failure);
        CHECK(reads > 0);
    }
}

#ifdef _DEBUG
// test::support::temporary_directory removes a populated directory through the error-code
// std::filesystem::remove_all overload while its destructor is implicitly noexcept. An
// allocation failure inside that cleanup is not a filesystem error, so it still escapes as
// std::bad_alloc and terminates the process. The build probe (build/main.py:
// _cleanup_fatal_probe) launches the hidden child below in its own process and requires the
// exact fatal exit code together with the directory the child could not remove; the process
// management lives in Python so the path handling stays Unicode-correct.
namespace
{
    // Distinct from Catch2's own 0..4 exit codes and from the _Exit(86) used elsewhere.
    constexpr int cleanup_fatal_exit_code = 0x5A;

    [[noreturn]] void cleanup_fatal_terminate_handler() noexcept
    {
        // std::exit, not _Exit or abort, so the LLVM profile runtime's atexit hook flushes
        // the child's coverage before the process ends.
        std::exit(cleanup_fatal_exit_code);
    }
} // namespace

TEST_CASE("directory cleanup: an allocation failure while removing a populated directory stays fatal",
          "[.directory-cleanup-fatal]")
{
    // Hidden (the tag begins with '.') so only the build probe runs this case, through an
    // explicit filter; the probe asserts the exact fatal exit code separately.
    std::optional<test::support::temporary_directory> directory;
    directory.emplace("cleanup-fatal");
    const auto nested = directory->path() / "nested.bin";
    {
        std::ofstream output(nested, std::ios::binary);
        REQUIRE(output.is_open());
        output << "nested";
        output.close();
        REQUIRE(output.good());
    }
    REQUIRE(std::filesystem::exists(nested));

    std::set_terminate(cleanup_fatal_terminate_handler);

    {
        // The first allocation after the hook is installed happens inside
        // std::filesystem::remove_all while it enumerates the populated directory, so the
        // refusal cannot land anywhere else; nothing between arming and reset() allocates.
        scoped_allocation_policy policy(1);
        directory.reset();
        // reset() must not return: the refused allocation escapes the noexcept destructor and
        // terminates with cleanup_fatal_exit_code. If it does return, the case ends normally
        // (exit code 0) and the probe's exact-code check fails, which is the intended signal.
    }
}
#endif
