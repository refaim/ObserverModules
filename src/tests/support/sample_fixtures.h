#ifndef UNPACKRAT_TESTS_SUPPORT_SAMPLE_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_SAMPLE_FIXTURES_H

#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace test::support
{
    // Encode interleaved PCM16 into a complete QOA stream using the pinned reference encoder.
    // ``interleaved`` holds exactly frames * channels samples; the LMS
    // state is seeded the same way Godot's importer seeds it. Throws std::invalid_argument on bad
    // arguments. The resulting stream is the canonical input a real QOA resource carries.
    [[nodiscard]] std::vector<std::byte> make_qoa_stream(std::uint32_t channels, std::uint32_t sample_rate,
                                                         std::span<const std::int16_t> interleaved);

    // A silent QOA stream of ``frames`` frames across ``channels`` channels.
    [[nodiscard]] std::vector<std::byte> qoa_silence(std::uint32_t channels, std::uint32_t sample_rate,
                                                     std::uint32_t frames);

    // Godot IMA ADPCM mono payload: the four zero prefix bytes Godot's writer emits followed by
    // ``packed`` nibble bytes. The prefix is data, not a header the decoder requires.
    [[nodiscard]] std::vector<std::byte> make_ima_mono(std::span<const std::byte> packed);

    // Godot IMA ADPCM stereo payload: two prefixed mono payloads interleaved byte for byte, exactly
    // as Godot's writer emits a stereo sample. ``left_packed`` and ``right_packed`` must match.
    [[nodiscard]] std::vector<std::byte> make_ima_stereo(std::span<const std::byte> left_packed,
                                                         std::span<const std::byte> right_packed);
} // namespace test::support
#endif
