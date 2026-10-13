#include "../../core/codecs/audio_preflight.h"
#include "../../core/codecs/godot_ima_codec.h"
#include "../../core/codecs/qoa_codec.h"
#include "../support/sample_fixtures.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <span>
#include <vector>

// The pinned reference decoder is linked from the QOA overlay; the tests drive it directly as an
// independent cross-check of the adapter's samples.
#define QOA_NO_STDIO
#include <qoa.h>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using unpackrat::codecs::audio_error;
    using unpackrat::codecs::audio_limits;
    using unpackrat::codecs::decode_godot_ima;
    using unpackrat::codecs::decode_qoa;
    using unpackrat::codecs::pcm_audio;

    // Largest per-sample difference between two equal-length PCM buffers.
    std::int32_t max_abs_error(const std::vector<std::int16_t> &lhs, const std::vector<std::int16_t> &rhs)
    {
        REQUIRE(lhs.size() == rhs.size());
        std::int32_t worst = 0;
        for (std::size_t index = 0; index < lhs.size(); ++index) {
            worst = std::max(worst, std::abs(static_cast<int>(lhs[index]) - static_cast<int>(rhs[index])));
        }
        return worst;
    }

    // Independent drive of the pinned reference decoder, mirroring Godot's frame loop.
    pcm_audio reference_decode_qoa(const std::span<const std::byte> bytes)
    {
        qoa_desc desc{};
        REQUIRE(qoa_decode_header(reinterpret_cast<const unsigned char *>(bytes.data()), static_cast<int>(bytes.size()),
                                  &desc) == 8);
        pcm_audio result;
        result.sample_rate = desc.samplerate;
        result.channels = desc.channels;
        result.samples.resize(static_cast<std::size_t>(desc.samples) * desc.channels);
        unsigned int offset = 8;
        unsigned int written = 0;
        do {
            unsigned int frame_len = 0;
            const unsigned int frame_size =
                qoa_decode_frame(reinterpret_cast<const unsigned char *>(bytes.data()) + offset,
                                 static_cast<unsigned int>(bytes.size()) - offset, &desc,
                                 result.samples.data() + static_cast<std::size_t>(written) * desc.channels, &frame_len);
            if (frame_size == 0) {
                break;
            }
            offset += frame_size;
            written += frame_len;
        } while (written < desc.samples);
        REQUIRE(written == desc.samples);
        return result;
    }

    // Byte offset of the second QOA frame, discovered through the reference decoder.
    std::size_t second_frame_offset(const std::span<const std::byte> bytes)
    {
        qoa_desc desc{};
        REQUIRE(qoa_decode_header(reinterpret_cast<const unsigned char *>(bytes.data()), static_cast<int>(bytes.size()),
                                  &desc) == 8);
        std::vector<std::int16_t> scratch(static_cast<std::size_t>(QOA_FRAME_LEN) * desc.channels);
        unsigned int frame_len = 0;
        const unsigned int frame_size =
            qoa_decode_frame(reinterpret_cast<const unsigned char *>(bytes.data()) + 8,
                             static_cast<unsigned int>(bytes.size()) - 8, &desc, scratch.data(), &frame_len);
        REQUIRE(frame_size != 0);
        return 8 + frame_size;
    }

    // Independent transcription of Godot's playback loop (AudioStreamPlaybackWAV::decode_samples),
    // phrased with the incremental last_nibble cursor the engine uses. It shares no code with the
    // adapter, so agreement is a real cross-check of the nibble order, interleave and clamps.
    std::vector<std::int16_t> godot_ima_reference(const std::span<const std::byte> payload,
                                                  const std::uint32_t channels)
    {
        constexpr std::array<std::int32_t, 89> step_table = {
            7,    8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,
            31,   34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
            130,  143,   157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,
            544,  598,   658,   724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
            2272, 2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,
            9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
        };
        constexpr std::array<std::int32_t, 16> index_table = {
            -1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8,
        };

        const auto *const data = reinterpret_cast<const std::uint8_t *>(payload.data());
        const std::size_t total_samples = payload.size() * 2;
        const std::size_t frames = total_samples / channels;
        std::vector<std::int16_t> out(total_samples);
        std::array<std::int32_t, 2> predictor{0, 0};
        std::array<std::int32_t, 2> step_index{0, 0};
        std::int64_t last_nibble = -1;
        for (std::size_t frame = 0; frame < frames; ++frame) {
            const std::int64_t pos = static_cast<std::int64_t>(frame);
            while (pos > last_nibble) {
                ++last_nibble;
                for (std::uint32_t channel = 0; channel < channels; ++channel) {
                    const std::uint8_t byte = data[static_cast<std::size_t>(last_nibble >> 1) * channels + channel];
                    const std::int32_t nibble = (last_nibble & 1) != 0 ? (byte >> 4) : (byte & 0x0F);
                    const std::int32_t step = step_table[static_cast<std::size_t>(step_index[channel])];
                    step_index[channel] += index_table[static_cast<std::size_t>(nibble)];
                    step_index[channel] = std::clamp(step_index[channel], 0, 88);
                    std::int32_t diff = step >> 3;
                    if ((nibble & 1) != 0) {
                        diff += step >> 2;
                    }
                    if ((nibble & 2) != 0) {
                        diff += step >> 1;
                    }
                    if ((nibble & 4) != 0) {
                        diff += step;
                    }
                    if ((nibble & 8) != 0) {
                        diff = -diff;
                    }
                    predictor[channel] = std::clamp(predictor[channel] + diff, -0x8000, 0x7FFF);
                }
            }
            out[frame * channels] = static_cast<std::int16_t>(predictor[0]);
            if (channels == 2) {
                out[frame * channels + 1] = static_cast<std::int16_t>(predictor[1]);
            }
        }
        return out;
    }

    std::vector<std::byte> pattern_payload(const std::size_t size, const std::uint32_t seed)
    {
        std::vector<std::byte> payload(size);
        std::uint32_t state = seed;
        for (auto &byte : payload) {
            state = state * 1664525U + 1013904223U;
            byte = static_cast<std::byte>(state >> 24);
        }
        return payload;
    }
} // namespace

TEST_CASE("sample: qoa silence decodes to the declared rate, channels and length")
{
    for (const std::uint32_t channels : {1U, 2U}) {
        const std::vector<std::byte> bytes = test::support::qoa_silence(channels, 44100, 100);
        const pcm_audio decoded = decode_qoa(bytes);
        CHECK(decoded.sample_rate == 44100);
        CHECK(decoded.channels == channels);
        REQUIRE(decoded.samples.size() == std::size_t{100} * channels);
        // QOA is lossy: even digital silence reconstructs to a one-LSB dequantization floor
        // (dequant_tab[scalefactor][0] is never zero), so silence must stay within +/-1 LSB.
        CHECK(std::all_of(decoded.samples.begin(), decoded.samples.end(),
                          [](const std::int16_t sample) { return std::abs(sample) <= 1; }));
    }
}

TEST_CASE("sample: qoa decode matches the reference decoder sample for sample")
{
    for (const std::uint32_t channels : {1U, 2U}) {
        std::vector<std::int16_t> source(std::size_t{6000} * channels);
        for (std::uint32_t frame = 0; frame < 6000U; ++frame) {
            const std::int16_t value = static_cast<std::int16_t>((static_cast<int>(frame % 128) - 64) * 20);
            for (std::uint32_t channel = 0; channel < channels; ++channel) {
                source[static_cast<std::size_t>(frame) * channels + channel] =
                    static_cast<std::int16_t>(value + static_cast<std::int16_t>(channel) * 7);
            }
        }
        const std::vector<std::byte> bytes = test::support::make_qoa_stream(channels, 32000, source);
        const pcm_audio decoded = decode_qoa(bytes);
        const pcm_audio expected = reference_decode_qoa(bytes);
        CHECK(decoded.sample_rate == expected.sample_rate);
        CHECK(decoded.channels == expected.channels);
        CHECK(decoded.samples == expected.samples);
        CHECK(decoded.samples.size() == source.size());
    }
}

TEST_CASE("sample: qoa lossy decode stays close to the source signal")
{
    std::vector<std::int16_t> source(6000U);
    for (std::uint32_t frame = 0; frame < 6000U; ++frame) {
        source[frame] = static_cast<std::int16_t>((static_cast<int>(frame % 512) - 256) * 2);
    }
    const pcm_audio decoded = decode_qoa(test::support::make_qoa_stream(1, 44100, source));
    REQUIRE(decoded.samples.size() == source.size());
    CHECK(max_abs_error(decoded.samples, source) <= 200);
}

TEST_CASE("sample: qoa rejects empty, garbage, truncated and unsupported input")
{
    CHECK_THROWS_AS(decode_qoa({}), audio_error);

    const std::vector<std::byte> garbage(64, std::byte{0x11});
    CHECK_THROWS_AS(decode_qoa(garbage), audio_error);

    // A stream whose file header is fine but that stops before the first frame header.
    std::vector<std::byte> header_only = test::support::qoa_silence(1, 44100, 64);
    header_only.resize(8);
    CHECK_THROWS_AS(decode_qoa(header_only), audio_error);

    // A two-frame stream cut inside the second frame's header, after the first frame decoded.
    std::vector<std::byte> truncated = test::support::qoa_silence(1, 44100, 6000);
    truncated.resize(second_frame_offset(truncated) + 4);
    CHECK_THROWS_AS(decode_qoa(truncated), audio_error);

    const std::vector<std::int16_t> three_channels(std::size_t{3} * 100, 0);
    CHECK_THROWS_AS(decode_qoa(test::support::make_qoa_stream(3, 44100, three_channels)), audio_error);
}

TEST_CASE("sample: qoa rejects inconsistent frame headers")
{
    // A frame that declares zero samples.
    std::vector<std::byte> zero_samples = test::support::qoa_silence(1, 44100, 6000);
    zero_samples[12] = std::byte{0x00};
    zero_samples[13] = std::byte{0x00};
    CHECK_THROWS_AS(decode_qoa(zero_samples), audio_error);

    // A frame that declares more samples than the file header.
    std::vector<std::byte> total_overflow = test::support::qoa_silence(1, 44100, 6000);
    total_overflow[12] = std::byte{0x17};
    total_overflow[13] = std::byte{0xE8}; // 0x17E8 = 6120 > 6000
    CHECK_THROWS_AS(decode_qoa(total_overflow), audio_error);

    // A frame whose declared size cannot even hold its LMS state.
    std::vector<std::byte> short_frame = test::support::qoa_silence(1, 44100, 6000);
    short_frame[14] = std::byte{0x00};
    short_frame[15] = std::byte{0x08};
    CHECK_THROWS_AS(decode_qoa(short_frame), audio_error);

    // A frame whose declared size runs past the end of the stream.
    std::vector<std::byte> size_overflow = test::support::qoa_silence(1, 44100, 100);
    size_overflow[14] = std::byte{0xFF};
    size_overflow[15] = std::byte{0xFF};
    CHECK_THROWS_AS(decode_qoa(size_overflow), audio_error);

    // A frame whose declared sample count exceeds the slices it actually carries, so the reference
    // decoder refuses it.
    std::vector<std::byte> slices_overflow = test::support::qoa_silence(1, 44100, 6000);
    slices_overflow[12] = std::byte{0x17};
    slices_overflow[13] = std::byte{0x70}; // 0x1770 = 6000 samples
    slices_overflow[14] = std::byte{0x00};
    slices_overflow[15] = std::byte{0x20}; // a 32-byte frame carries one 20-sample slice
    CHECK_THROWS_AS(decode_qoa(slices_overflow), audio_error);

    // A later frame whose channel count disagrees with the file header.
    std::vector<std::byte> channel_mismatch = test::support::qoa_silence(1, 44100, 6000);
    channel_mismatch[second_frame_offset(channel_mismatch)] = std::byte{2};
    CHECK_THROWS_AS(decode_qoa(channel_mismatch), audio_error);

    // A later frame whose sample rate disagrees with the file header.
    std::vector<std::byte> rate_mismatch = test::support::qoa_silence(1, 44100, 6000);
    const std::size_t second = second_frame_offset(rate_mismatch);
    rate_mismatch[second + 1] = std::byte{0x00};
    rate_mismatch[second + 2] = std::byte{0x56};
    rate_mismatch[second + 3] = std::byte{0x2A}; // 22050 != 44100
    CHECK_THROWS_AS(decode_qoa(rate_mismatch), audio_error);
}

TEST_CASE("sample: qoa rejects a frame whose size cannot hold the slices it declares")
{
    // A single stereo sample still needs one full slice per channel: 8 header + 16*2 LMS + 8*2
    // slices = 56 bytes. A frame that declares only 48 leaves the second channel's slice missing,
    // and the reference decoder would read it past the end of the stream.
    std::vector<std::byte> truncated_stereo = test::support::qoa_silence(2, 44100, 1);
    REQUIRE(truncated_stereo.size() == 8 + 56);
    truncated_stereo[14] = std::byte{0x00};
    truncated_stereo[15] = std::byte{0x30}; // frame size 48, one slice short for stereo
    truncated_stereo.resize(8 + 48);
    CHECK_THROWS_AS(decode_qoa(truncated_stereo), audio_error);

    // A frame that declares more samples than a single frame can carry, even though the slices it
    // happens to fund would cover them.
    std::vector<std::byte> oversized_frame = test::support::qoa_silence(1, 44100, 6000);
    oversized_frame[12] = std::byte{0x14};
    oversized_frame[13] = std::byte{0x01}; // 0x1401 = 5121 samples, one past QOA_FRAME_LEN
    CHECK_THROWS_AS(decode_qoa(oversized_frame), audio_error);

    // A well-formed frame whose declared size is correct but runs past the available bytes.
    std::vector<std::byte> cut_slices = test::support::qoa_silence(1, 44100, 5120);
    REQUIRE(cut_slices.size() == 8 + 8 + 16 + 256 * 8);
    cut_slices.resize(8 + 8 + 16 + 100);
    CHECK_THROWS_AS(decode_qoa(cut_slices), audio_error);

    // A well-formed frame that declares more samples than the file header can still hold, even
    // though the count is a legal single-frame length.
    std::vector<std::byte> header_overflow = test::support::qoa_silence(1, 44100, 10);
    header_overflow[12] = std::byte{0x14};
    header_overflow[13] = std::byte{0x00}; // 0x1400 = 5120 samples against a 10-sample file
    CHECK_THROWS_AS(decode_qoa(header_overflow), audio_error);
}

TEST_CASE("sample: qoa decodes a valid partial stereo frame and matches the reference decoder")
{
    // The smallest legal stereo frame carries a single sample per channel in one slice each. It is
    // a valid partial frame and must round-trip through the reference decoder.
    const std::vector<std::byte> bytes = test::support::qoa_silence(2, 44100, 1);
    const pcm_audio decoded = decode_qoa(bytes);
    const pcm_audio expected = reference_decode_qoa(bytes);
    CHECK(decoded.channels == 2);
    CHECK(decoded.samples == expected.samples);
    REQUIRE(decoded.samples.size() == 2);
    CHECK(std::all_of(decoded.samples.begin(), decoded.samples.end(),
                      [](const std::int16_t sample) { return std::abs(sample) <= 1; }));
}

TEST_CASE("sample: qoa enforces the input, frame and byte budgets")
{
    const std::vector<std::byte> bytes = test::support::qoa_silence(1, 44100, 100);

    audio_limits input_limits;
    input_limits.max_input_bytes = bytes.size() - 1;
    CHECK_THROWS_AS(decode_qoa(bytes, input_limits), audio_error);

    audio_limits frame_limits;
    frame_limits.max_pcm_frames = 99;
    CHECK_THROWS_AS(decode_qoa(bytes, frame_limits), audio_error);

    audio_limits byte_limits;
    byte_limits.max_output_bytes = 100U * sizeof(std::int16_t) - 1;
    CHECK_THROWS_AS(decode_qoa(bytes, byte_limits), audio_error);
}

TEST_CASE("sample: godot ima matches an independent transcription of the engine decoder")
{
    for (const std::uint32_t channels : {1U, 2U}) {
        for (const std::uint32_t seed : {0U, 1U, 2U, 7U, 1234U}) {
            const std::vector<std::byte> packed = pattern_payload(32, seed);
            const std::vector<std::byte> payload =
                channels == 2 ? test::support::make_ima_stereo(packed, pattern_payload(32, seed + 99U))
                              : test::support::make_ima_mono(packed);
            const pcm_audio decoded = decode_godot_ima(payload, channels, 22050);
            CHECK(decoded.samples == godot_ima_reference(payload, channels));
        }
    }
}

TEST_CASE("sample: godot ima mono reproduces the official nibble order, step index and clamps")
{
    constexpr std::array<std::byte, 14> packed = {
        std::byte{0x1F}, std::byte{0x70}, std::byte{0x80}, std::byte{0x07}, std::byte{0xF0},
        std::byte{0x77}, std::byte{0x77}, std::byte{0x77}, std::byte{0x77}, std::byte{0x77},
        std::byte{0x77}, std::byte{0x77}, std::byte{0x77}, std::byte{0x77},
    };
    const std::vector<std::byte> payload = test::support::make_ima_mono(packed);
    REQUIRE(payload.size() == 18);

    const pcm_audio decoded = decode_godot_ima(payload, 1, 22050);
    CHECK(decoded.sample_rate == 22050);
    CHECK(decoded.channels == 1);
    const std::vector<std::int16_t> expected = {
        0,     0,     0,     0,     0,     0,     0,     0,     -11,   -5,    -4,    19,
        22,    19,    60,    66,    71,    -5,    160,   515,   1280,  2922,  6442,  13990,
        30170, 32767, 32767, 32767, 32767, 32767, 32767, 32767, 32767, 32767, 32767, 32767,
    };
    CHECK(decoded.samples == expected);
    CHECK(decoded.samples == godot_ima_reference(payload, 1));
}

TEST_CASE("sample: godot ima stereo interleaves two packed mono lanes byte for byte")
{
    constexpr std::array<std::byte, 3> left = {std::byte{0x1F}, std::byte{0x70}, std::byte{0x88}};
    constexpr std::array<std::byte, 3> right = {std::byte{0x77}, std::byte{0x00}, std::byte{0xF0}};
    const std::vector<std::byte> payload = test::support::make_ima_stereo(left, right);
    REQUIRE(payload.size() == 14);

    const pcm_audio decoded = decode_godot_ima(payload, 2, 44100);
    CHECK(decoded.sample_rate == 44100);
    CHECK(decoded.channels == 2);
    const std::vector<std::int16_t> expected = {
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -11, 11, -5, 41, -4, 45, 19, 48, 16, 51, 13, 5,
    };
    CHECK(decoded.samples == expected);
    CHECK(decoded.samples == godot_ima_reference(payload, 2));
}

TEST_CASE("sample: godot ima clamps the predictor at both rails")
{
    constexpr std::array<std::byte, 8> positive = {
        std::byte{0x77}, std::byte{0x77}, std::byte{0x77}, std::byte{0x77},
        std::byte{0x77}, std::byte{0x77}, std::byte{0x77}, std::byte{0x77},
    };
    constexpr std::array<std::byte, 8> negative = {
        std::byte{0xFF}, std::byte{0xFF}, std::byte{0xFF}, std::byte{0xFF},
        std::byte{0xFF}, std::byte{0xFF}, std::byte{0xFF}, std::byte{0xFF},
    };

    const pcm_audio upper = decode_godot_ima(test::support::make_ima_mono(positive), 1, 8000);
    const std::vector<std::int16_t> upper_expected = {
        0,   0,    0,    0,    0,     0,     0,     0,     11,    41,    104,   240,
        533, 1164, 2521, 5431, 11667, 25039, 32767, 32767, 32767, 32767, 32767, 32767,
    };
    CHECK(upper.samples == upper_expected);

    const pcm_audio lower = decode_godot_ima(test::support::make_ima_mono(negative), 1, 8000);
    const std::vector<std::int16_t> lower_expected = {
        0,    0,     0,     0,     0,      0,      0,      0,      -11,    -41,    -104,   -240,
        -533, -1164, -2521, -5431, -11667, -25039, -32768, -32768, -32768, -32768, -32768, -32768,
    };
    CHECK(lower.samples == lower_expected);
}

TEST_CASE("sample: godot ima seed bytes decode as silence")
{
    const std::vector<std::byte> mono_seed(4, std::byte{0});
    const pcm_audio mono = decode_godot_ima(mono_seed, 1, 8000);
    REQUIRE(mono.samples.size() == 8);
    CHECK(std::all_of(mono.samples.begin(), mono.samples.end(), [](const std::int16_t sample) { return sample == 0; }));

    const std::vector<std::byte> stereo_seed(8, std::byte{0});
    const pcm_audio stereo = decode_godot_ima(stereo_seed, 2, 8000);
    REQUIRE(stereo.samples.size() == 16);
    CHECK(std::all_of(stereo.samples.begin(), stereo.samples.end(),
                      [](const std::int16_t sample) { return sample == 0; }));
}

TEST_CASE("sample: godot ima rejects malformed channels, rate and size")
{
    const std::vector<std::byte> payload = test::support::make_ima_mono(
        std::array<std::byte, 4>{std::byte{0x1F}, std::byte{0x70}, std::byte{0x80}, std::byte{0x07}});

    CHECK_THROWS_AS(decode_godot_ima(payload, 0, 8000), audio_error);
    CHECK_THROWS_AS(decode_godot_ima(payload, 3, 8000), audio_error);
    CHECK_THROWS_AS(decode_godot_ima(payload, 1, 0), audio_error);

    // Mono needs at least one payload byte and stereo a whole interleaved pair; anything shorter
    // has no valid frame at all. A stereo pair that is an odd number of bytes cannot be
    // byte-interleaved.
    CHECK_THROWS_AS(decode_godot_ima({}, 1, 8000), audio_error);
    CHECK_THROWS_AS(decode_godot_ima({}, 2, 8000), audio_error);
    const std::vector<std::byte> one_byte(1, std::byte{0});
    CHECK_THROWS_AS(decode_godot_ima(one_byte, 2, 8000), audio_error);
    const std::vector<std::byte> odd_stereo(9, std::byte{0});
    CHECK_THROWS_AS(decode_godot_ima(odd_stereo, 2, 8000), audio_error);
}

TEST_CASE("sample: incremental Godot IMA decoder rejects inconsistent chunk geometry", "[ima-chunk]")
{
    using unpackrat::codecs::decode_godot_ima_chunk;
    using unpackrat::codecs::godot_ima_decoder_state;
    const std::array packed{std::byte{0x17}};
    std::array<std::int16_t, 2> output{};
    godot_ima_decoder_state state{};

    REQUIRE_THROWS_AS(decode_godot_ima_chunk(packed, 0, 1, state, std::span(output).first(1)), audio_error);
    REQUIRE_THROWS_AS(decode_godot_ima_chunk(packed, 3, 1, state, output), audio_error);
    REQUIRE_THROWS_AS(decode_godot_ima_chunk(packed, 1, 0, state, std::span(output).first(1)), audio_error);
    REQUIRE_THROWS_AS(decode_godot_ima_chunk(packed, 2, (std::numeric_limits<std::size_t>::max)(), state, output),
                      audio_error);
    REQUIRE_THROWS_AS(decode_godot_ima_chunk(packed, 1, 1, state, output), audio_error);
    REQUIRE_THROWS_AS(decode_godot_ima_chunk({}, 1, 1, state, std::span(output).first(1)), audio_error);
    REQUIRE_NOTHROW(decode_godot_ima_chunk(packed, 1, 1, state, std::span(output).first(1)));
}

TEST_CASE("sample: godot ima decodes a payload that is shorter than the old seed prefix")
{
    // Godot's playback decoder starts from predictor and step index zero and treats the provider
    // bytes as ordinary ADPCM data, so a mono payload is valid from its very first byte and a
    // stereo payload from its first interleaved pair. The historical four zero bytes are data, not
    // a required header.
    const std::vector<std::byte> mono = {std::byte{0x1F}};
    const pcm_audio mono_decoded = decode_godot_ima(mono, 1, 8000);
    REQUIRE(mono_decoded.samples.size() == 2);
    CHECK(mono_decoded.samples == godot_ima_reference(mono, 1));

    const std::vector<std::byte> stereo = {std::byte{0x1F}, std::byte{0x70}};
    const pcm_audio stereo_decoded = decode_godot_ima(stereo, 2, 8000);
    REQUIRE(stereo_decoded.samples.size() == 4);
    CHECK(stereo_decoded.samples == godot_ima_reference(stereo, 2));
}

TEST_CASE("sample: godot ima enforces the input, frame and byte budgets")
{
    const std::vector<std::byte> payload = test::support::make_ima_mono(std::array<std::byte, 6>{
        std::byte{0x1F}, std::byte{0x70}, std::byte{0x80}, std::byte{0x07}, std::byte{0xF0}, std::byte{0x77}});

    audio_limits input_limits;
    input_limits.max_input_bytes = payload.size() - 1;
    CHECK_THROWS_AS(decode_godot_ima(payload, 1, 8000, input_limits), audio_error);

    audio_limits frame_limits;
    frame_limits.max_pcm_frames = payload.size() * 2U - 1;
    CHECK_THROWS_AS(decode_godot_ima(payload, 1, 8000, frame_limits), audio_error);

    audio_limits byte_limits;
    byte_limits.max_output_bytes = payload.size() * 2U * sizeof(std::int16_t) - 1;
    CHECK_THROWS_AS(decode_godot_ima(payload, 1, 8000, byte_limits), audio_error);
}

TEST_CASE("sample: the interleaved sample preflight rejects counts a size_t cannot hold")
{
    using unpackrat::codecs::preflight::interleaved_sample_count;

    // The boundary is expressed through the host's own size_t width so the same assertions stay
    // correct on a 32-bit and a 64-bit build rather than assuming a 64-bit host.
    constexpr std::uint64_t limit = std::numeric_limits<std::size_t>::max();

    // The exact product is returned while it is representable, right up to the largest count a
    // size_t can hold, so the count is decided before any multiply-driven allocation.
    CHECK(interleaved_sample_count(0, 2) == 0);
    CHECK(interleaved_sample_count(6000, 2) == 12000);
    CHECK(interleaved_sample_count(limit / 2, 2) == limit - 1);

    // A product past the representable range is refused rather than wrapping to a small count.
    CHECK_THROWS_AS(interleaved_sample_count(limit / 2 + 1, 2), audio_error);
    CHECK_THROWS_AS(interleaved_sample_count(limit, 2), audio_error);
}

TEST_CASE("sample: Godot IMA scalar capacity rejects an overflowing metadata length before multiplication",
          "[sample][ima-capacity]")
{
    using unpackrat::codecs::preflight::ima_frame_capacity;
    constexpr std::uint64_t largest_safe_bytes = (std::numeric_limits<std::uint64_t>::max)() / 2;
    CHECK(ima_frame_capacity(1, 1) == 2);
    CHECK(ima_frame_capacity(2, 2) == 2);
    CHECK(ima_frame_capacity(largest_safe_bytes, 1) == largest_safe_bytes * 2);
    CHECK_THROWS_AS(ima_frame_capacity(largest_safe_bytes + 1, 1), audio_error);
    CHECK_THROWS_AS(ima_frame_capacity(1, 0), audio_error);
    CHECK_THROWS_AS(ima_frame_capacity(1, 3), audio_error);
}

TEST_CASE("sample: godot ima decodes exactly the declared frame count without the padding nibble")
{
    // Three headerless mono bytes carry six nibble frames. Declaring five must decode exactly five
    // frames, leaving the final padding nibble neither decoded nor allocated.
    const std::array<std::byte, 3> packed = {std::byte{0x01}, std::byte{0x23}, std::byte{0x45}};
    const std::vector<std::int16_t> reference = godot_ima_reference(packed, 1);
    REQUIRE(reference.size() == 6);

    const pcm_audio full = decode_godot_ima(packed, 1, 8000);
    REQUIRE(full.samples.size() == 6);
    CHECK(full.samples == reference);

    const pcm_audio five = decode_godot_ima(packed, 1, 8000, 5);
    REQUIRE(five.samples.size() == 5);
    CHECK(five.samples == std::vector<std::int16_t>(reference.begin(), reference.begin() + 5));

    // A declared count equal to the capacity decodes the whole payload.
    CHECK(decode_godot_ima(packed, 1, 8000, 6).samples == reference);

    // The frame budget is applied to the declared count: five fits, four does not.
    audio_limits exact;
    exact.max_pcm_frames = 5;
    CHECK(decode_godot_ima(packed, 1, 8000, 5, exact).samples.size() == 5);
    audio_limits tight;
    tight.max_pcm_frames = 4;
    CHECK_THROWS_AS(decode_godot_ima(packed, 1, 8000, 5, tight), audio_error);

    // A declared count beyond the capacity or more than one below it is malformed.
    CHECK_THROWS_AS(decode_godot_ima(packed, 1, 8000, 7), audio_error);
    CHECK_THROWS_AS(decode_godot_ima(packed, 1, 8000, 2), audio_error);
}

TEST_CASE("sample: godot ima declared frame count still rejects a structurally invalid payload")
{
    // A stereo payload that is an odd number of bytes cannot be byte-interleaved, with or without a
    // declared frame count.
    const std::vector<std::byte> odd_stereo(9, std::byte{0});
    CHECK_THROWS_AS(decode_godot_ima(odd_stereo, 2, 8000, 3), audio_error);

    // A payload shorter than one interleaved frame is rejected before the declared count is used.
    const std::vector<std::byte> one_byte(1, std::byte{0});
    CHECK_THROWS_AS(decode_godot_ima(one_byte, 2, 8000, 1), audio_error);
}

TEST_CASE("sample: an explicit Godot IMA declared frame count of zero is rejected", "[sample][r3f3]")
{
    // The declared-count overload promises a positive source length, so zero is not a legal count and
    // must not fall back to decoding the whole padded capacity. The whole-payload overload is the one
    // that decodes every encoded frame.
    const std::array<std::byte, 3> packed = {std::byte{0x01}, std::byte{0x23}, std::byte{0x45}};
    CHECK_THROWS_AS(decode_godot_ima(packed, 1, 8000, 0), audio_error);

    const std::vector<std::int16_t> reference = godot_ima_reference(packed, 1);
    REQUIRE(reference.size() == 6);
    CHECK(decode_godot_ima(packed, 1, 8000).samples == reference);
}
