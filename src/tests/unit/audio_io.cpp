#include "../../core/codecs/audio_preflight.h"
#include "../../core/codecs/mp3_codec.h"
#include "../../core/codecs/ogg_codec.h"
#include "../../core/codecs/qoa_codec.h"
#include "../../core/codecs/wav_codec.h"
#include "../support/audio_fixtures.h"
#include "../support/sample_fixtures.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

#ifdef _WIN64
#include <Windows.h>
#endif

// The adapter links the decoder/writer implementations; the tests drive the vendored libraries
// directly as an independent cross-check of the bytes the adapter produces.
#include <../third_party/dr_libs/dr_mp3.h>
#include <../third_party/dr_libs/dr_wav.h>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using unpackrat::codecs::audio_error;
    using unpackrat::codecs::audio_limits;
    using unpackrat::codecs::decode_mp3;
    using unpackrat::codecs::mp3_stream_info;
    using unpackrat::codecs::pcm_audio;
    using unpackrat::codecs::probe_mp3;
    using unpackrat::codecs::write_wav;

    // MPEG-1 Layer III always decodes 1152 PCM frames per MP3 frame.
    constexpr std::size_t samples_per_mp3_frame = 1152;

    class sink_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Independent dr_mp3 drive: decode the whole stream with the raw library API. Returns nullopt
    // when the library rejects the stream outright, so the adapter can be compared under exactly
    // the same accept/reject decision the library makes.
    std::optional<pcm_audio> read_mp3_directly(const std::span<const std::byte> bytes)
    {
        struct decoder_context final
        {
            decoder_context() = default;
            decoder_context(const decoder_context &) = delete;
            decoder_context &operator=(const decoder_context &) = delete;

            std::unique_ptr<drmp3> decoder = std::make_unique<drmp3>();
            bool initialized = false;

            ~decoder_context()
            {
                if (initialized)
                    drmp3_uninit(decoder.get());
            }
        } context;
        drmp3 &decoder = *context.decoder;
        context.initialized = drmp3_init_memory(&decoder, bytes.data(), bytes.size(), nullptr) == DRMP3_TRUE;
        if (!context.initialized) {
            return std::nullopt;
        }
        pcm_audio result;
        result.sample_rate = decoder.sampleRate;
        result.channels = decoder.channels;
        std::vector<drmp3_int16> chunk(std::size_t{1152} * decoder.channels);
        for (;;) {
            const drmp3_uint64 read = drmp3_read_pcm_frames_s16(&decoder, 1152, chunk.data());
            if (read == 0) {
                break;
            }
            result.samples.insert(result.samples.end(), chunk.begin(),
                                  chunk.begin() + static_cast<std::ptrdiff_t>(read * decoder.channels));
        }
        return result;
    }

    // A free-format MPEG-1 Layer III stream: the same 417-byte silence frames the repository-owned
    // fixture builds, but with the header bitrate index cleared to 0. A free-format frame carries no
    // bitrate, so dr_mp3 learns the frame size from the spacing between the sync words; three or more
    // matching complete frames are required before the library confirms that size.
    std::vector<std::byte> make_free_format_mp3(const std::uint32_t frame_count, const bool stereo)
    {
        std::vector<std::byte> stream = test::support::make_mp3_silence(frame_count, stereo);
        const std::size_t frame_bytes = stream.size() / frame_count;
        for (std::size_t offset = 0; offset + 4 <= stream.size(); offset += frame_bytes) {
            stream[offset + 2] = std::byte{0x00}; // bitrate index 0 (free format), 44100 Hz, no padding
        }
        return stream;
    }

    // Independent dr_wav read of the produced stream.
    std::vector<std::int16_t> read_wav_directly(const std::span<const std::byte> bytes, std::uint32_t &rate,
                                                std::uint32_t &channels)
    {
        drwav wav{};
        REQUIRE(drwav_init_memory(&wav, bytes.data(), bytes.size(), nullptr) == DRWAV_TRUE);
        rate = wav.sampleRate;
        channels = wav.channels;
        REQUIRE(wav.bitsPerSample == 16);
        std::vector<std::int16_t> samples(static_cast<std::size_t>(wav.totalPCMFrameCount) * wav.channels);
        const drwav_uint64 frames = drwav_read_pcm_frames_s16(&wav, wav.totalPCMFrameCount, samples.data());
        samples.resize(static_cast<std::size_t>(frames) * wav.channels);
        drwav_uninit(&wav);
        return samples;
    }

    pcm_audio make_tone(const std::uint32_t channels, const std::vector<std::int16_t> &samples)
    {
        pcm_audio audio;
        audio.sample_rate = 44100;
        audio.channels = channels;
        audio.samples = samples;
        return audio;
    }

    std::vector<std::byte> collect_wav(const pcm_audio &audio, const audio_limits &limits = {},
                                       std::vector<std::size_t> *const chunk_sizes = nullptr)
    {
        std::vector<std::byte> collected;
        write_wav(
            audio,
            [&collected, chunk_sizes](const std::span<const std::byte> chunk) {
                if (chunk_sizes != nullptr) {
                    chunk_sizes->push_back(chunk.size());
                }
                collected.insert(collected.end(), chunk.begin(), chunk.end());
            },
            limits);
        return collected;
    }
} // namespace

TEST_CASE("audio: mp3 silence decodes to a known rate, channel count and length")
{
    const pcm_audio mono = decode_mp3(test::support::make_mp3_silence(4, false));
    CHECK(mono.sample_rate == 44100);
    CHECK(mono.channels == 1);
    REQUIRE(mono.samples.size() == 4 * samples_per_mp3_frame);
    CHECK(std::all_of(mono.samples.begin(), mono.samples.end(), [](const std::int16_t sample) { return sample == 0; }));

    const pcm_audio stereo = decode_mp3(test::support::make_mp3_silence(4, true));
    CHECK(stereo.sample_rate == 44100);
    CHECK(stereo.channels == 2);
    REQUIRE(stereo.samples.size() == std::size_t{2} * 4 * samples_per_mp3_frame);
    CHECK(std::all_of(stereo.samples.begin(), stereo.samples.end(),
                      [](const std::int16_t sample) { return sample == 0; }));
}

TEST_CASE("sample: an unrestricted input budget still probes a small MP3 frame", "[sample][mp3-large-limit]")
{
    const std::vector<std::byte> frame = test::support::make_mp3_silence(1, false);
    audio_limits limits;
    limits.max_input_bytes = (std::numeric_limits<std::size_t>::max)();
    const mp3_stream_info info = probe_mp3(frame, limits);
    REQUIRE(info.frame_bytes == frame.size());
    REQUIRE(info.channels == 1);
    REQUIRE(info.sample_rate == 44100);
}

TEST_CASE("audio: mp3 decode matches an independent dr_mp3 read")
{
    for (const bool stereo : {false, true}) {
        const std::vector<std::byte> bytes = test::support::make_mp3_silence(6, stereo);
        const pcm_audio decoded = decode_mp3(bytes);
        const std::optional<pcm_audio> expected = read_mp3_directly(bytes);
        REQUIRE(expected.has_value());
        const pcm_audio expected_audio = expected.value_or(pcm_audio{});
        CHECK(decoded.sample_rate == expected_audio.sample_rate);
        CHECK(decoded.channels == expected_audio.channels);
        CHECK(decoded.samples == expected_audio.samples);
    }
}

TEST_CASE("audio: mp3 rejects empty and undecodable input")
{
    CHECK_THROWS_AS(decode_mp3({}), audio_error);

    const std::vector<std::byte> garbage(64, std::byte{0x11});
    REQUIRE(!(read_mp3_directly(garbage).has_value()));
    CHECK_THROWS_AS(decode_mp3(garbage), audio_error);
}

TEST_CASE("audio: mp3 handles a truncated stream exactly as the reference library does")
{
    // One whole frame followed by a second frame cut short. The adapter adds no validation the
    // library does not perform: it accepts or rejects the stream exactly where dr_mp3 does, and a
    // decoded result is sample-for-sample identical and a whole number of frames.
    std::vector<std::byte> truncated = test::support::make_mp3_silence(2, false);
    truncated.resize(truncated.size() - 100);

    const std::optional<pcm_audio> expected = read_mp3_directly(truncated);
    if (!expected.has_value()) {
        CHECK_THROWS_AS(decode_mp3(truncated), audio_error);
    } else {
        const pcm_audio decoded = decode_mp3(truncated);
        CHECK(decoded.channels == expected->channels);
        CHECK(decoded.samples == expected->samples);
        CHECK(decoded.samples.size() % decoded.channels == 0);
    }
}

TEST_CASE("audio: mp3 decode enforces the input, frame and byte budgets")
{
    const std::vector<std::byte> bytes = test::support::make_mp3_silence(4, false);

    audio_limits input_limits;
    input_limits.max_input_bytes = bytes.size() - 1;
    CHECK_THROWS_AS(decode_mp3(bytes, input_limits), audio_error);

    audio_limits frame_limits;
    frame_limits.max_pcm_frames = 4 * samples_per_mp3_frame - 1;
    CHECK_THROWS_AS(decode_mp3(bytes, frame_limits), audio_error);

    audio_limits byte_limits;
    byte_limits.max_output_bytes = 100;
    CHECK_THROWS_AS(decode_mp3(bytes, byte_limits), audio_error);
}

TEST_CASE("audio: wav writer emits a canonical PCM16 RIFF stream")
{
    const pcm_audio audio = make_tone(1, {0, 1, -1, 32767, -32768, 12345});
    std::vector<std::size_t> chunk_sizes;
    const std::vector<std::byte> collected = collect_wav(audio, {}, &chunk_sizes);

    // The stream is a 44-byte header followed by the raw PCM payload, written sequentially. The
    // writer emits the header in several small pieces and then the payload in a single chunk.
    std::size_t total_written = 0;
    for (const std::size_t size : chunk_sizes) {
        total_written += size;
    }
    REQUIRE(total_written == 44 + audio.samples.size() * 2);
    REQUIRE(chunk_sizes.back() == audio.samples.size() * 2);
    REQUIRE(collected.size() == 44 + audio.samples.size() * 2);
    const test::support::wav_view view = test::support::parse_pcm16_wav(collected);
    CHECK(view.format_tag == 1);
    CHECK(view.channels == 1);
    CHECK(view.sample_rate == 44100);
    CHECK(view.byte_rate == 44100 * 2);
    CHECK(view.block_align == 2);
    CHECK(view.bits_per_sample == 16);
    CHECK(view.samples == audio.samples);

    std::uint32_t rate = 0;
    std::uint32_t channels = 0;
    CHECK(read_wav_directly(collected, rate, channels) == audio.samples);
    CHECK(rate == 44100);
    CHECK(channels == 1);
}

TEST_CASE("audio: wav writer emits interleaved stereo samples")
{
    const pcm_audio audio = make_tone(2, {1, 2, 3, 4, 5, 6});
    const std::vector<std::byte> collected = collect_wav(audio);

    const test::support::wav_view view = test::support::parse_pcm16_wav(collected);
    CHECK(view.channels == 2);
    CHECK(view.byte_rate == 44100 * 4);
    CHECK(view.block_align == 4);
    CHECK(view.samples == audio.samples);

    std::uint32_t rate = 0;
    std::uint32_t channels = 0;
    CHECK(read_wav_directly(collected, rate, channels) == audio.samples);
    CHECK(channels == 2);
}

TEST_CASE("audio: wav writer emits an empty data chunk for empty audio")
{
    const pcm_audio audio = make_tone(1, {});
    const std::vector<std::byte> collected = collect_wav(audio);

    REQUIRE(collected.size() == 44);
    const test::support::wav_view view = test::support::parse_pcm16_wav(collected);
    CHECK(view.samples.empty());
    CHECK(view.channels == 1);
}

TEST_CASE("audio: wav writer surfaces a budget breach on a header-only empty stream")
{
    // An empty stream's 44-byte header still counts against the output budget. dr_wav reports a
    // short write through its sequential finalizer, but an empty stream's zero-frame target is
    // already satisfied, so the callback's sticky budget flag must surface the refusal instead of
    // the stream reporting success.
    pcm_audio audio;
    audio.sample_rate = 8000;
    audio.channels = 1;
    REQUIRE(audio.samples.empty());

    std::size_t calls = 0;
    const auto sink = [&calls](const std::span<const std::byte>) { ++calls; };

    audio_limits zero;
    zero.max_output_bytes = 0;
    CHECK_THROWS_AS(write_wav(audio, sink, zero), audio_error);

    // One byte short of the header is still a breach.
    audio_limits one_short;
    one_short.max_output_bytes = 43;
    CHECK_THROWS_AS(write_wav(audio, sink, one_short), audio_error);

    // Exactly the header fits, so the empty stream is emitted as a valid 44-byte WAV.
    audio_limits exact;
    exact.max_output_bytes = 44;
    const std::vector<std::byte> collected = collect_wav(audio, exact);
    REQUIRE(collected.size() == 44);
    const test::support::wav_view view = test::support::parse_pcm16_wav(collected);
    CHECK(view.format_tag == 1);
    CHECK(view.channels == 1);
    CHECK(view.sample_rate == 8000);
    CHECK(view.samples.empty());
}

TEST_CASE("audio: wav writer rejects unsupported formats before writing")
{
    std::size_t writes = 0;
    const auto sink = [&writes](const std::span<const std::byte>) { ++writes; };

    CHECK_THROWS_AS(write_wav(make_tone(0, {}), sink), audio_error);
    CHECK_THROWS_AS(write_wav(make_tone(3, {1, 2, 3, 4, 5, 6}), sink), audio_error);

    pcm_audio zero_rate = make_tone(1, {1, 2});
    zero_rate.sample_rate = 0;
    CHECK_THROWS_AS(write_wav(zero_rate, sink), audio_error);

    CHECK_THROWS_AS(write_wav(make_tone(2, {1, 2, 3}), sink), audio_error);
    CHECK(writes == 0);
}

TEST_CASE("audio: wav writer enforces the frame and byte budgets before writing")
{
    std::size_t writes = 0;
    const auto sink = [&writes](const std::span<const std::byte>) { ++writes; };

    audio_limits frame_limits;
    frame_limits.max_pcm_frames = 1;
    CHECK_THROWS_AS(write_wav(make_tone(1, {1, 2, 3}), sink, frame_limits), audio_error);

    audio_limits byte_limits;
    byte_limits.max_output_bytes = 2;
    CHECK_THROWS_AS(write_wav(make_tone(1, {1, 2}), sink, byte_limits), audio_error);
    CHECK(writes == 0);
}

TEST_CASE("audio: wav writer bounds the whole stream, header included")
{
    // Four mono samples fit the payload budget, but the 44-byte header pushes the total stream one
    // byte past the limit, so the writer's stream callback must reject the data write.
    const pcm_audio audio = make_tone(1, {1, 2, 3, 4});
    audio_limits limits;
    limits.max_output_bytes = 44 + audio.samples.size() * 2 - 1;
    CHECK_THROWS_AS(collect_wav(audio, limits), audio_error);
}

TEST_CASE("audio: wav writer refuses a stream whose header already exceeds the budget")
{
    // The budget is smaller than the 44-byte header, so the callback rejects partway through the
    // header and every later header write must be refused without entering the sink again.
    const pcm_audio audio = make_tone(1, {1, 2, 3, 4});
    audio_limits limits;
    limits.max_output_bytes = 10;
    std::size_t calls = 0;
    CHECK_THROWS_AS(write_wav(audio, [&calls](const std::span<const std::byte>) { ++calls; }, limits), audio_error);
    CHECK(calls >= 1);
}

TEST_CASE("audio: wav writer propagates a header sink failure and stays reusable")
{
    const pcm_audio audio = make_tone(1, {7, 8, 9, 10});

    int calls = 0;
    try {
        write_wav(audio, [&calls](const std::span<const std::byte>) {
            ++calls;
            throw sink_error("header rejected");
        });
        FAIL("expected the sink exception to propagate");
    } catch (const sink_error &error) {
        CHECK(std::string(error.what()) == "header rejected");
    }
    CHECK(calls >= 1);

    // No global state survives, so a later call yields a fresh, valid stream.
    const std::vector<std::byte> retry = collect_wav(audio);
    CHECK(test::support::parse_pcm16_wav(retry).samples == audio.samples);
}

TEST_CASE("audio: mp3 probe rejects a junk prefix before the first frame", "[audio][mp3][r2f3]")
{
    // The public contract is an optional ID3v2 tag immediately followed by an MPEG frame. A scanner
    // may skip junk to reach a frame, but that input is not the contract, so the probe must reject
    // it instead of reporting a frame size that folds in the skipped bytes.
    const std::vector<std::byte> frame = test::support::make_mp3_silence(1, false);
    std::vector<std::byte> junked = {std::byte{'A'}, std::byte{'B'}, std::byte{'C'}};
    junked.insert(junked.end(), frame.begin(), frame.end());
    junked.insert(junked.end(), frame.begin(), frame.end());
    CHECK_THROWS_AS(probe_mp3(junked), audio_error);
}

TEST_CASE("audio: mp3 probe accepts a valid ID3v2 tag immediately before the frame", "[audio][mp3][r2f3]")
{
    const std::vector<std::byte> frame = test::support::make_mp3_silence(1, false);
    std::vector<std::byte> tagged(10, std::byte{0});
    tagged[0] = std::byte{'I'};
    tagged[1] = std::byte{'D'};
    tagged[2] = std::byte{'3'};
    tagged[3] = std::byte{4};
    tagged.insert(tagged.end(), frame.begin(), frame.end());
    const mp3_stream_info info = probe_mp3(tagged);
    CHECK(info.sample_rate == 44100);
    CHECK(info.channels == 1);
    CHECK(info.frame_bytes == frame.size());
}

TEST_CASE("audio: mp3 probe reconstructs the exact first-frame size across MPEG variants", "[audio][mp3][r2f3]")
{
    // An MPEG-2 Layer III frame packs 576 PCM frames, so the offset check must not assume the
    // MPEG-1 1152 count. 64 kbit/s at 22050 Hz -> floor(576 * 64 * 125 / 22050) = 208 bytes.
    std::vector<std::byte> mpeg2(208, std::byte{0});
    mpeg2[0] = std::byte{0xFF};
    mpeg2[1] = std::byte{0xF3};
    mpeg2[2] = std::byte{0x80};
    mpeg2[3] = std::byte{0xC0};
    const mp3_stream_info info2 = probe_mp3(mpeg2);
    CHECK(info2.sample_rate == 22050);
    CHECK(info2.channels == 1);
    CHECK(info2.frame_bytes == 208);

    // A padded MPEG-1 Layer III frame carries one extra padding slot byte: 417 + 1 = 418.
    std::vector<std::byte> padded(418, std::byte{0});
    padded[0] = std::byte{0xFF};
    padded[1] = std::byte{0xFB};
    padded[2] = std::byte{0x92};
    padded[3] = std::byte{0xC0};
    const mp3_stream_info info1 = probe_mp3(padded);
    CHECK(info1.sample_rate == 44100);
    CHECK(info1.channels == 1);
    CHECK(info1.frame_bytes == 418);
}

TEST_CASE("audio: mp3 probe recognises a complete Layer III frame and reports metadata")
{
    const std::vector<std::byte> mono = test::support::make_mp3_silence(1, false);
    const mp3_stream_info mono_info = probe_mp3(mono);
    CHECK(mono_info.sample_rate == 44100);
    CHECK(mono_info.channels == 1);
    CHECK(mono_info.frame_bytes == mono.size());

    const std::vector<std::byte> stereo = test::support::make_mp3_silence(1, true);
    const mp3_stream_info stereo_info = probe_mp3(stereo);
    CHECK(stereo_info.sample_rate == 44100);
    CHECK(stereo_info.channels == 2);
    CHECK(stereo_info.frame_bytes == stereo.size());
}

TEST_CASE("audio: mp3 probe skips a bounded ID3v2 tag, with and without a footer")
{
    const std::vector<std::byte> frame = test::support::make_mp3_silence(1, false);

    std::vector<std::byte> tagged(10, std::byte{0});
    tagged[0] = std::byte{'I'};
    tagged[1] = std::byte{'D'};
    tagged[2] = std::byte{'3'};
    tagged[3] = std::byte{4};
    tagged.insert(tagged.end(), frame.begin(), frame.end());
    const mp3_stream_info info = probe_mp3(tagged);
    CHECK(info.frame_bytes == frame.size());
    CHECK(info.channels == 1);

    // The footer flag adds a second ten-byte block after the tag body.
    std::vector<std::byte> footered(20, std::byte{0});
    footered[0] = std::byte{'I'};
    footered[1] = std::byte{'D'};
    footered[2] = std::byte{'3'};
    footered[3] = std::byte{4};
    footered[5] = std::byte{0x10};
    footered.insert(footered.end(), frame.begin(), frame.end());
    CHECK(probe_mp3(footered).frame_bytes == frame.size());
}

TEST_CASE("audio: mp3 probe rejects empty, over-budget and malformed input")
{
    CHECK_THROWS_AS(probe_mp3({}), audio_error);

    const std::vector<std::byte> frame = test::support::make_mp3_silence(1, false);
    audio_limits tight;
    tight.max_input_bytes = frame.size() - 1;
    CHECK_THROWS_AS(probe_mp3(frame, tight), audio_error);

    // The decoder's signed address range constrains the actual span, not a looser caller budget.
    audio_limits huge;
    huge.max_input_bytes = static_cast<std::size_t>(std::numeric_limits<int>::max()) + 1;
    CHECK(probe_mp3(frame, huge).frame_bytes == frame.size());

    // An ID3 size byte with the high bit set, an ID3 tag that runs past the input, and a tag that
    // leaves too few bytes for a frame.
    std::vector<std::byte> high_bit(15, std::byte{0});
    high_bit[0] = std::byte{'I'};
    high_bit[1] = std::byte{'D'};
    high_bit[2] = std::byte{'3'};
    high_bit[3] = std::byte{4};
    high_bit[6] = std::byte{0x80};
    CHECK_THROWS_AS(probe_mp3(high_bit), audio_error);

    std::vector<std::byte> oversized(12, std::byte{0});
    oversized[0] = std::byte{'I'};
    oversized[1] = std::byte{'D'};
    oversized[2] = std::byte{'3'};
    oversized[3] = std::byte{4};
    oversized[6] = std::byte{0x7F};
    oversized[7] = std::byte{0x7F};
    oversized[8] = std::byte{0x7F};
    oversized[9] = std::byte{0x7F};
    CHECK_THROWS_AS(probe_mp3(oversized), audio_error);

    std::vector<std::byte> header_only(10, std::byte{0});
    header_only[0] = std::byte{'I'};
    header_only[1] = std::byte{'D'};
    header_only[2] = std::byte{'3'};
    header_only[3] = std::byte{4};
    CHECK_THROWS_AS(probe_mp3(header_only), audio_error);
}

TEST_CASE("audio: mp3 probe requires a complete Layer III frame")
{
    // One whole frame followed by nothing else is accepted; cut short it is refused.
    std::vector<std::byte> truncated = test::support::make_mp3_silence(1, false);
    truncated.resize(16);
    CHECK_THROWS_AS(probe_mp3(truncated), audio_error);

    // A complete, correctly sized MPEG-1 Layer II frame (0xFD: MPEG-1, Layer II, 44100 Hz,
    // 160 kbit/s -> floor(1152 * 160 * 125 / 44100) = 522 bytes) is recognised as a frame but is
    // refused because only Layer III is supported. Without the layer gate it would pass.
    std::vector<std::byte> layer2(522, std::byte{0});
    layer2[0] = std::byte{0xFF};
    layer2[1] = std::byte{0xFD};
    layer2[2] = std::byte{0x90};
    layer2[3] = std::byte{0xC0};
    CHECK_THROWS_AS(probe_mp3(layer2), audio_error);
}

TEST_CASE("audio: mp3 probe recognises a free-format Layer III stream", "[audio][mp3][r3f2]")
{
    // Bitrate index 0 means the frame carries no bitrate field; the decoder confirms the size from
    // the sync-word spacing and reports it in free_format_bytes, which the probe must reproduce
    // instead of folding in the zero bitrate.
    const std::vector<std::byte> mono = make_free_format_mp3(4, false);
    const mp3_stream_info mono_info = probe_mp3(mono);
    CHECK(mono_info.sample_rate == 44100);
    CHECK(mono_info.channels == 1);
    CHECK(mono_info.frame_bytes == mono.size() / 4);

    const std::vector<std::byte> stereo = make_free_format_mp3(4, true);
    const mp3_stream_info stereo_info = probe_mp3(stereo);
    CHECK(stereo_info.sample_rate == 44100);
    CHECK(stereo_info.channels == 2);
    CHECK(stereo_info.frame_bytes == stereo.size() / 4);
}

TEST_CASE("audio: wav writer keeps the first sink failure and stops calling the sink")
{
    const pcm_audio audio = make_tone(1, {1, 2, 3, 4, 5, 6, 7, 8});
    // dr_wav keeps invoking the write callback after a short write, so the callback must latch the
    // first failure: the exception raised on the first call is the one that reaches the caller and
    // the sink is never entered again.
    int calls = 0;
    try {
        write_wav(audio, [&calls](const std::span<const std::byte>) {
            ++calls;
            if (calls == 1) {
                throw sink_error("first");
            }
            throw std::runtime_error("later");
        });
        FAIL("expected the first sink exception to propagate");
    } catch (const sink_error &error) {
        CHECK(std::string(error.what()) == "first");
    }
    CHECK(calls == 1);
}

TEST_CASE("audio: wav writer rejects a sample rate whose format byte rate overflows")
{
    std::size_t writes = 0;
    const auto sink = [&writes](const std::span<const std::byte>) { ++writes; };

    // dr_wav derives the fmt byte rate with 32-bit arithmetic as (16 * sampleRate * channels) / 8,
    // so a mono rate of 2^28 wraps that product to zero. The writer must reject it up front.
    pcm_audio overflow = make_tone(1, {1, 2});
    overflow.sample_rate = 268435456U;
    CHECK_THROWS_AS(write_wav(overflow, sink), audio_error);
    CHECK(writes == 0);

    // The largest mono rate the product still holds, and the byte rate the header must carry.
    pcm_audio boundary = make_tone(1, {1, 2});
    boundary.sample_rate = 268435455U;
    const test::support::wav_view view = test::support::parse_pcm16_wav(collect_wav(boundary));
    CHECK(view.sample_rate == 268435455U);
    CHECK(view.byte_rate == 536870910U);
}

TEST_CASE("audio: the riff payload preflight caps a stream at the container limit")
{
    using unpackrat::codecs::preflight::riff_pcm16_stream_bytes;

    // The helper is pure format sizing, so its result is a 64-bit value by contract; a 32-bit
    // return would narrow a legal >4 GiB stream.
    static_assert(std::is_same_v<decltype(riff_pcm16_stream_bytes(0, 1)), std::uint64_t>,
                  "riff stream sizing must stay 64-bit");

    // The canonical stream size is the 44-byte header plus the interleaved PCM16 payload, and the
    // largest payload a RIFF data chunk can describe is UINT32_MAX - 36 bytes.
    CHECK(riff_pcm16_stream_bytes(0, 1) == 44);
    CHECK(riff_pcm16_stream_bytes(5, 2) == 44 + 5 * 2 * 2);

    // The largest legal mono stream is a little over 4 GiB; the helper reports the exact 64-bit
    // format size rather than narrowing it to a 32-bit size_t.
    const std::uint64_t mono_limit = (0xFFFFFFFFULL - 36) / 2;
    CHECK(riff_pcm16_stream_bytes(mono_limit, 1) == 44 + mono_limit * 2);
    CHECK(riff_pcm16_stream_bytes(mono_limit, 1) == 4294967302ULL);
    CHECK_THROWS_AS(riff_pcm16_stream_bytes(mono_limit + 1, 1), audio_error);

    // Only the mono and stereo PCM16 geometry this phase supports is a describable RIFF stream, and
    // the lane count is pinned before any multiply, so a degenerate or huge channel count cannot
    // overflow the sizing arithmetic.
    CHECK_THROWS_AS(riff_pcm16_stream_bytes(1, 0), audio_error);
    CHECK_THROWS_AS(riff_pcm16_stream_bytes(1, 3), audio_error);
    CHECK_THROWS_AS(riff_pcm16_stream_bytes(0, 3), audio_error);
    CHECK_THROWS_AS(riff_pcm16_stream_bytes(std::numeric_limits<std::uint64_t>::max(), 2), audio_error);
    CHECK_THROWS_AS(riff_pcm16_stream_bytes(std::numeric_limits<std::uint64_t>::max(), 0), audio_error);
}

TEST_CASE("audio: wav writer propagates a data sink failure and stays reusable")
{
    const pcm_audio audio = make_tone(1, {1, 2, 3, 4, 5, 6, 7, 8});
    // The 44-byte header is delivered as a run of small chunks and the PCM payload follows; the sink
    // rejects the first byte past the header, so this exercises the data-phase failure specifically.
    std::size_t written = 0;
    try {
        write_wav(audio, [&written](const std::span<const std::byte> chunk) {
            if (written >= 44) {
                throw sink_error("data rejected");
            }
            written += chunk.size();
        });
        FAIL("expected the sink exception to propagate");
    } catch (const sink_error &error) {
        CHECK(std::string(error.what()) == "data rejected");
    }
    CHECK(written == 44);

    const std::vector<std::byte> retry = collect_wav(audio);
    CHECK(test::support::parse_pcm16_wav(retry).samples == audio.samples);
}

TEST_CASE("audio: WAV streaming validates geometry and explicit limits before output", "[wav-streaming]")
{
    std::uint64_t frames = 1;
    std::uint32_t channels = 1, rate = 44100;
    audio_limits limits;
    SECTION("zero channels")
    {
        channels = 0;
    }
    SECTION("unsupported channels")
    {
        channels = 3;
    }
    SECTION("zero sample rate")
    {
        rate = 0;
    }
    SECTION("byte rate overflows")
    {
        rate = 0xFFFFFFFFU;
    }
    SECTION("frame budget")
    {
        limits.max_pcm_frames = 0;
    }
    SECTION("complete stream byte budget")
    {
        limits.max_output_bytes = 45;
    }
    SECTION("RIFF payload cannot represent the declared frame count")
    {
        frames = 0xFFFFFFFFULL;
    }
    std::size_t pulls = 0, writes = 0;
    CHECK_THROWS_AS(unpackrat::codecs::write_wav_streaming(
                        frames, channels, rate, [&](std::uint64_t, std::span<std::int16_t>) { ++pulls; },
                        [&](std::span<const std::byte>) { ++writes; }, limits),
                    audio_error);
    CHECK(pulls == 0);
    CHECK(writes == 0);
}

TEST_CASE("audio: WAV header sink failure stops before pulling samples", "[wav-streaming]")
{
    std::size_t pulls = 0;
    CHECK_THROWS_AS(unpackrat::codecs::write_wav_streaming(
                        1, 1, 44100,
                        [&](std::uint64_t, const std::span<std::int16_t> samples) {
                            ++pulls;
                            std::fill(samples.begin(), samples.end(), 0);
                        },
                        [](std::span<const std::byte>) { throw sink_error("header sink failure"); }),
                    sink_error);
    CHECK(pulls == 0);
}

TEST_CASE("audio: WAV streaming emits bounded PCM chunks and preserves callback failures", "[wav-streaming]")
{
    SECTION("multiple chunks produce an independently readable stereo stream")
    {
        std::vector<std::byte> output;
        std::size_t pulls = 0;
        unpackrat::codecs::write_wav_streaming(
            4097, 2, 22050,
            [&](const std::uint64_t offset, const std::span<std::int16_t> samples) {
                CHECK(samples.size() <= 4096);
                CHECK(offset == std::uint64_t{2048} * pulls);
                ++pulls;
                for (std::size_t i = 0; i < samples.size(); ++i)
                    samples[i] = static_cast<std::int16_t>((offset * 2 + i) % 1000);
            },
            [&](const std::span<const std::byte> bytes) { output.insert(output.end(), bytes.begin(), bytes.end()); });
        const auto wav = test::support::parse_pcm16_wav(output);
        CHECK(pulls == 3);
        CHECK(wav.channels == 2);
        CHECK(wav.sample_rate == 22050);
        REQUIRE(wav.samples.size() == 8194);
        for (std::size_t i = 0; i < wav.samples.size(); ++i)
            CHECK(wav.samples[i] == static_cast<std::int16_t>(i % 1000));
    }
    SECTION("empty PCM writes a valid header without a pull")
    {
        std::vector<std::byte> output;
        unpackrat::codecs::write_wav_streaming(
            0, 1, 44100, [](std::uint64_t, std::span<std::int16_t>) { FAIL("empty stream pulled samples"); },
            [&](const std::span<const std::byte> bytes) { output.insert(output.end(), bytes.begin(), bytes.end()); });
        CHECK(test::support::parse_pcm16_wav(output).samples.empty());
    }
    SECTION("pull exception propagates unchanged")
    {
        CHECK_THROWS_AS(unpackrat::codecs::write_wav_streaming(
                            1, 1, 44100, [](std::uint64_t, std::span<std::int16_t>) { throw sink_error("pull"); },
                            [](std::span<const std::byte>) {}),
                        sink_error);
    }
    SECTION("PCM sink exception propagates unchanged")
    {
        std::size_t bytes = 0;
        CHECK_THROWS_AS(unpackrat::codecs::write_wav_streaming(
                            1, 1, 44100,
                            [](std::uint64_t, const std::span<std::int16_t> samples) {
                                std::fill(samples.begin(), samples.end(), 42);
                            },
                            [&](const std::span<const std::byte> chunk) {
                                if (bytes >= 44)
                                    throw sink_error("PCM");
                                bytes += chunk.size();
                            }),
                        sink_error);
        CHECK(bytes == 44);
    }
}

TEST_CASE("audio: QOA frame decoder validates every descriptor and storage boundary", "[qoa-frame]")
{
    const auto stream = test::support::qoa_silence(1, 44100, 1);
    std::vector<std::byte> frame(stream.begin() + 8, stream.end());
    std::uint32_t channels = 1, rate = 44100;
    std::vector<std::int16_t> output(1, 1234);
    SECTION("zero channels")
    {
        channels = 0;
    }
    SECTION("unsupported channels")
    {
        channels = 3;
    }
    SECTION("zero sample rate")
    {
        rate = 0;
    }
    SECTION("truncated fixed header")
    {
        frame.resize(7);
    }
    SECTION("frame channel mismatch")
    {
        frame[0] = std::byte{2};
    }
    SECTION("frame sample-rate mismatch")
    {
        frame[3] = std::byte{0x45};
    }
    SECTION("zero frame samples")
    {
        frame[5] = std::byte{0};
    }
    SECTION("sample count exceeds QOA frame length")
    {
        frame[4] = std::byte{0x14};
        frame[5] = std::byte{1};
    }
    SECTION("output shorter than declared samples")
    {
        output.clear();
    }
    SECTION("declared frame length mismatch")
    {
        frame[7] = std::byte{31};
    }
    SECTION("physical frame length mismatch")
    {
        frame.pop_back();
    }
    CHECK_THROWS_AS(unpackrat::codecs::decode_qoa_frame(frame, channels, rate, output), audio_error);
    CHECK(std::all_of(output.begin(), output.end(), [](std::int16_t sample) { return sample == 1234; }));
}

TEST_CASE("audio: QOA frame decoder matches the full reference-backed decoder", "[qoa-frame]")
{
    for (const std::uint32_t channels : {1U, 2U}) {
        for (const std::uint32_t frames : {1U, 20U, 5120U}) {
            const auto stream = test::support::qoa_silence(channels, 44100, frames);
            const auto expected = unpackrat::codecs::decode_qoa(stream);
            std::vector<std::int16_t> output(static_cast<std::size_t>(frames) * channels + 2, 1234);
            const auto count = unpackrat::codecs::decode_qoa_frame(std::span<const std::byte>(stream).subspan(8),
                                                                   channels, 44100, output);
            CHECK(count == frames);
            CHECK(std::equal(expected.samples.begin(), expected.samples.end(), output.begin()));
            CHECK(output[output.size() - 1] == 1234);
            CHECK(output[output.size() - 2] == 1234);
        }
    }
}

#ifdef _WIN64
TEST_CASE("audio: MP3 probe accepts a sparse mapped stream beyond the signed decoder window", "[mp3-sparse-probe]")
{
    // Reserve a real contiguous mapped byte range without allocating two gigabytes of RAM. Only
    // the first page is committed: a header probe must inspect that prefix and bound the signed
    // window handed to dr_mp3. The previous whole-input INT_MAX rejection fails this test.
    constexpr std::uint64_t logical_size = static_cast<std::uint64_t>((std::numeric_limits<int>::max)()) + 1;
    const HANDLE mapping =
        CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE | SEC_RESERVE,
                           static_cast<DWORD>(logical_size >> 32), static_cast<DWORD>(logical_size), nullptr);
    REQUIRE(mapping != nullptr);
    if (mapping == nullptr) {
        return;
    }
    const auto close_mapping = std::unique_ptr<void, decltype(&CloseHandle)>(mapping, CloseHandle);
    void *const view = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, static_cast<SIZE_T>(logical_size));
    REQUIRE(view != nullptr);
    const auto unmap_view = std::unique_ptr<void, decltype(&UnmapViewOfFile)>(view, UnmapViewOfFile);
    REQUIRE(VirtualAlloc(view, 65536, MEM_COMMIT, PAGE_READWRITE) == view);
    const auto frames = test::support::make_mp3_silence(11, false);
    std::copy(frames.begin(), frames.end(), static_cast<std::byte *>(view));
    const auto mapped =
        std::span<const std::byte>(static_cast<const std::byte *>(view), static_cast<std::size_t>(logical_size));
    const mp3_stream_info info = probe_mp3(mapped);
    REQUIRE(info.frame_bytes == frames.size() / 11);
    REQUIRE(info.sample_rate == 44100);
    REQUIRE(info.channels == 1);
}
#endif

TEST_CASE("audio: source MP3 probe rejects an empty or over-budget source before reading", "[mp3-source-bounds]")
{
    std::size_t reads = 0;
    const auto read = [&](const std::uint64_t, const std::span<std::byte>) { ++reads; };
    const unpackrat::io::byte_source empty{0, read};
    CHECK_THROWS_AS(unpackrat::codecs::probe_mp3_source(empty), audio_error);
    CHECK(reads == 0);

    const unpackrat::io::byte_source oversized{101, read};
    audio_limits limit;
    limit.max_input_bytes = 100;
    CHECK_THROWS_AS(unpackrat::codecs::probe_mp3_source(oversized, limit), audio_error);
    CHECK(reads == 0);
}

TEST_CASE("audio: MP3 source probe keeps its bounded frame window off the callback stack", "[mp3-stack]")
{
    const auto frames = test::support::make_mp3_silence(11, false);
    std::byte outer_marker{};
    const auto outer_address = reinterpret_cast<std::uintptr_t>(&outer_marker);
    std::uintptr_t farthest = 0;
    const unpackrat::io::byte_source source{
        frames.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> destination) {
            std::byte inner_marker{};
            const auto inner_address = reinterpret_cast<std::uintptr_t>(&inner_marker);
            farthest = (std::max)(farthest, outer_address > inner_address ? outer_address - inner_address
                                                                          : inner_address - outer_address);
            REQUIRE(offset <= frames.size());
            REQUIRE(destination.size() <= frames.size() - offset);
            std::copy_n(std::span<const std::byte>(frames).subspan(static_cast<std::size_t>(offset)).begin(),
                        destination.size(), destination.begin());
        },
    };
    const auto info = unpackrat::codecs::probe_mp3_source(source);
    INFO(farthest);
    CHECK(info.sample_rate == 44100);
    CHECK(info.channels == 1);
    CHECK(farthest < std::uintptr_t{20} * 1024);
}

#ifdef _WIN64
TEST_CASE("audio: Ogg rejects a real mapped packet beyond libogg's signed-long range before reading it",
          "[ogg-long-bounds]")
{
    constexpr std::uint64_t logical_size = static_cast<std::uint64_t>((std::numeric_limits<long>::max)()) + 1;
    const HANDLE mapping =
        CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE | SEC_RESERVE,
                           static_cast<DWORD>(logical_size >> 32), static_cast<DWORD>(logical_size), nullptr);
    REQUIRE(mapping != nullptr);
    if (mapping == nullptr) {
        return;
    }
    const auto close_mapping = std::unique_ptr<void, decltype(&CloseHandle)>(mapping, CloseHandle);
    void *const view = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, static_cast<SIZE_T>(logical_size));
    REQUIRE(view != nullptr);
    const auto unmap_view = std::unique_ptr<void, decltype(&UnmapViewOfFile)>(view, UnmapViewOfFile);

    const unpackrat::codecs::ogg_packet_view packet{
        std::span<const std::byte>(static_cast<const std::byte *>(view), static_cast<std::size_t>(logical_size)), 0};
    std::size_t writes = 0;
    CHECK_THROWS_AS(
        unpackrat::codecs::write_ogg(std::span(&packet, 1), [&](const std::span<const std::byte>) { ++writes; }),
        unpackrat::codecs::ogg_error);
    CHECK(writes == 0);
}
#endif
