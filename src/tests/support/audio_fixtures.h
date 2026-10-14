#ifndef UNPACKRAT_TESTS_SUPPORT_AUDIO_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_AUDIO_FIXTURES_H

#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace test::support
{
    // Build ``frame_count`` consecutive MPEG-1 Layer III frames that carry digital silence (zero
    // side info and main data). Every frame is a standard 128 kbit/s 44100 Hz frame that decodes to
    // 1152 PCM frames; ``stereo`` selects a two-channel stream, otherwise the stream is mono.
    [[nodiscard]] std::vector<std::byte> make_mp3_silence(std::uint32_t frame_count, bool stereo);

    // Canonical PCM16 RIFF/WAVE layout produced by the writer adapter.
    struct wav_view
    {
        std::uint16_t format_tag = 0;
        std::uint16_t channels = 0;
        std::uint32_t sample_rate = 0;
        std::uint32_t byte_rate = 0;
        std::uint16_t block_align = 0;
        std::uint16_t bits_per_sample = 0;
        std::vector<std::int16_t> samples;
    };

    // Parse the single ``fmt ``/``data`` PCM16 RIFF/WAVE stream the adapter emits. Throws
    // ``std::runtime_error`` when the structure or its sizes do not match.
    [[nodiscard]] wav_view parse_pcm16_wav(std::span<const std::byte> bytes);
} // namespace test::support
#endif
