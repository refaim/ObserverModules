#pragma once

#include "audio_types.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

// Godot's IMA ADPCM sample payload is not the Microsoft WAVE block format. The whole payload is a
// single ADPCM run that the playback decoder starts from a zero predictor and step index, and a
// stereo payload is two packed mono runs interleaved byte for byte. Godot's writer happens to
// prefix each lane with four zero bytes, but that prefix is ordinary data, not a required header.
// This is a narrow, MIT-licensed adaptation of the official writer and playback decoder in Godot
// 4.5-stable:
//   godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d
//   scene/resources/audio_stream_wav.cpp
//     sha256 11874a0fd2d814edc266a095b3a61a71aa24b0a1e6795109a4446706e32b6aec
//   scene/resources/audio_stream_wav.h
//     sha256 88112b489e445177ae1984700289156a1daefa5c46d2ec3b3e4d4fd60be79156
//   MIT (c) 2007-2014 Juan Linietsky, Ariel Manzur and contributors; the full MIT text is
//   reproduced in godot_ima_codec.cpp so the notice survives wherever this code ships.
//   Cross-checked against the independent converter in the MIT-licensed Godot RE Tools exporter,
//   gdre/exporters/sample_exporter.cpp
//     sha256 64982d14a0dae04d4ab7de5163d2438247fcde0543ba172da3f49f54e7cda6b3.
namespace unpackrat::codecs
{
    struct godot_ima_channel_state final
    {
        std::int32_t predictor = 0;
        std::int32_t step_index = 0;
    };

    struct godot_ima_decoder_state final
    {
        std::array<godot_ima_channel_state, 2> channels{};
    };

    // Decode a consecutive, byte-aligned run of frames, retaining predictor and index between
    // chunks. All non-final chunks must contain an even number of frames.
    void decode_godot_ima_chunk(std::span<const std::byte> packed, std::uint32_t channels, std::size_t frames,
                                godot_ima_decoder_state &state, std::span<std::int16_t> output);
    // Decode a Godot IMA ADPCM payload into interleaved PCM16. ``channels`` is one or two and
    // ``sample_rate`` is the rate Godot stored beside the payload (the payload itself carries no
    // header fields, so the caller supplies both). The decoded sample count follows from the
    // payload size; every size, limit, and channel invariant is checked before allocation, so a
    // malformed payload raises ``audio_error``.
    [[nodiscard]] pcm_audio decode_godot_ima(std::span<const std::byte> input, std::uint32_t channels,
                                             std::uint32_t sample_rate, const audio_limits &limits = {});

    // Decode exactly ``declared_frames`` frames of a Godot IMA payload whose source declares a
    // positive frame count. The Godot 2 ``Sample`` writer packs two nibbles into every byte, so an
    // odd declared run leaves one padding nibble: ``declared_frames`` may be the encoded capacity
    // or exactly one frame below it, and the padding nibble is neither decoded nor allocated. Any
    // other count, including zero (not a positive length), and every structural, limit, and channel
    // invariant the whole-payload decode shares, raises ``audio_error`` before any allocation. The
    // whole-payload overload above keeps its behavior and is the one that decodes the full capacity;
    // this overload is only used when a source declares its own positive count.
    [[nodiscard]] pcm_audio decode_godot_ima(std::span<const std::byte> input, std::uint32_t channels,
                                             std::uint32_t sample_rate, std::uint64_t declared_frames,
                                             const audio_limits &limits = {});
} // namespace unpackrat::codecs
