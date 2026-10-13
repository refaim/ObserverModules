#include "godot_ima_codec.h"

#include "audio_preflight.h"

#include <array>
#include <cstdint>
#include <limits>
#include <optional>

// Godot's IMA ADPCM sample payload, adapted from Godot 4.5-stable (see godot_ima_codec.h for the
// pinned paths and hashes). The layout and nibble order come from the official writer
// (scene/resources/audio_stream_wav.h, _compress_ima_adpcm) and the official playback decoder
// (scene/resources/audio_stream_wav.cpp, AudioStreamPlaybackWAV::decode_samples). The full MIT
// notice from Godot is reproduced here so it survives wherever this file is bundled:
//
//   MIT License
//
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//
//   Permission is hereby granted, free of charge, to any person obtaining a copy
//   of this software and associated documentation files (the "Software"), to deal
//   in the Software without restriction, including without limitation the rights
//   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//   copies of the Software, and to permit persons to whom the Software is
//   furnished to do so, subject to the following conditions:
//
//   The above copyright notice and this permission notice shall be included in all
//   copies or substantial portions of the Software.
//
//   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//   SOFTWARE.
namespace unpackrat::codecs
{
    namespace
    {
        // Copied verbatim from Godot's IMA ADPCM decoder tables (audio_stream_wav.cpp/.h).
        constexpr std::array<std::int32_t, 89> ima_step_table = {
            7,    8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,
            31,   34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
            130,  143,   157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,
            544,  598,   658,   724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
            2272, 2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,
            9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
        };
        constexpr std::array<std::int32_t, 16> ima_index_table = {
            -1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8,
        };
        constexpr std::int32_t ima_max_step_index = 88;

        using adpcm_channel_state = godot_ima_channel_state;

        // Advances one nibble exactly as Godot's playback decoder does: the current step is read
        // before the index moves, the index is clamped to [0, 88], and the predictor to int16.
        void decode_nibble(const std::uint8_t packed, const bool high_nibble, adpcm_channel_state &state) noexcept
        {
            const std::uint8_t nibble =
                high_nibble ? static_cast<std::uint8_t>(packed >> 4) : static_cast<std::uint8_t>(packed & 0x0fU);
            const std::int32_t step = ima_step_table[static_cast<std::size_t>(state.step_index)];
            std::int32_t index = state.step_index + ima_index_table[static_cast<std::size_t>(nibble)];
            index = index < 0 ? 0 : (index > ima_max_step_index ? ima_max_step_index : index);
            state.step_index = index;

            std::int32_t difference = step >> 3;
            if ((nibble & 1) != 0) {
                difference += step >> 2;
            }
            if ((nibble & 2) != 0) {
                difference += step >> 1;
            }
            if ((nibble & 4) != 0) {
                difference += step;
            }
            if ((nibble & 8) != 0) {
                difference = -difference;
            }

            std::int32_t predictor = state.predictor + difference;
            if (predictor < -0x8000) {
                predictor = -0x8000;
            } else if (predictor > 0x7fff) {
                predictor = 0x7fff;
            }
            state.predictor = predictor;
        }
    } // namespace

    void decode_godot_ima_chunk(const std::span<const std::byte> packed, const std::uint32_t channels,
                                const std::size_t frames, godot_ima_decoder_state &state,
                                const std::span<std::int16_t> output)
    {
        if ((channels != 1 && channels != 2) || frames == 0 ||
            frames > ((std::numeric_limits<std::size_t>::max)() / channels) || output.size() != frames * channels ||
            packed.size() != ((frames + 1) / 2) * channels) {
            throw audio_error("godot-ima: a streaming chunk has invalid geometry");
        }
        for (std::size_t frame = 0; frame < frames; ++frame) {
            const std::size_t base = (frame / 2) * channels;
            for (std::size_t channel = 0; channel < channels; ++channel) {
                decode_nibble(std::to_integer<std::uint8_t>(packed[base + channel]), (frame & 1) != 0,
                              state.channels[channel]);
                output[frame * channels + channel] = static_cast<std::int16_t>(state.channels[channel].predictor);
            }
        }
    }

    namespace
    {
        // Shared implementation behind both public overloads. An empty ``declared_frames`` decodes
        // the whole encoded capacity; a present value promises a positive source length and must be
        // the capacity or one frame below it, so only the source layout's single legal trailing
        // padding nibble may be left undecoded. Zero is not a length and is rejected rather than
        // silently decoded as the whole capacity.
        pcm_audio decode_ima(const std::span<const std::byte> input, const std::uint32_t channels,
                             const std::uint32_t sample_rate, const std::optional<std::uint64_t> declared_frames,
                             const audio_limits &limits)
        {
            if (channels != 1 && channels != 2) {
                throw audio_error("godot-ima: only mono and stereo payloads are supported");
            }
            if (sample_rate == 0) {
                throw audio_error("godot-ima: the sample rate must be non-zero");
            }
            if (input.size() > limits.max_input_bytes) {
                throw audio_error("godot-ima: input exceeds the configured input byte limit");
            }
            // The payload carries no header: Godot's playback decoder starts from predictor and step
            // index zero and treats the provider bytes as ordinary ADPCM data, so the historical four
            // zero seed bytes are data, not a required prefix. A mono payload needs at least one byte
            // and a stereo payload a whole interleaved pair.
            if (input.size() < channels) {
                throw audio_error("godot-ima: the payload is shorter than one interleaved frame");
            }
            if (channels == 2 && input.size() % 2 != 0) {
                throw audio_error("godot-ima: a stereo payload must be byte-interleaved");
            }

            // Each byte carries two nibbles, so the payload capacity is twice its size in samples
            // spread over the interleaved lanes. The count is derived from the untrusted payload
            // length, so its representability is checked before the frame count follows from it.
            const std::size_t capacity = preflight::interleaved_sample_count(input.size(), 2) / channels;
            std::size_t frames = capacity;
            if (declared_frames.has_value()) {
                // The declared-count overload promises a positive length, so zero is not a legal
                // count: it would otherwise decode the whole padded capacity, exactly the behavior
                // the whole-payload overload already owns.
                if (*declared_frames == 0) {
                    throw audio_error("godot-ima: a declared frame count must be positive");
                }
                // Only the capacity or one frame below it is a legal declaration: anything shorter
                // drops real audio and anything larger would need padding that was never written.
                if (*declared_frames > capacity || *declared_frames + 1 < capacity) {
                    throw audio_error("godot-ima: the declared frame count disagrees with the encoded capacity");
                }
                frames = static_cast<std::size_t>(*declared_frames);
            }
            if (frames > limits.max_pcm_frames) {
                throw audio_error("godot-ima: the frame count exceeds the configured limit");
            }
            // Only the frames actually decoded are allocated, so a declared count that trims the
            // padding nibble never reserves memory for a frame that is not produced.
            const std::size_t total_samples = frames * channels;
            if (total_samples > limits.max_output_bytes / sizeof(std::int16_t)) {
                throw audio_error("godot-ima: decoded size exceeds the configured output byte limit");
            }

            pcm_audio result;
            result.sample_rate = sample_rate;
            result.channels = channels;
            result.samples.resize(total_samples);

            const auto *const data = reinterpret_cast<const std::uint8_t *>(input.data());
            std::array<adpcm_channel_state, 2> state{};
            for (std::size_t frame = 0; frame < frames; ++frame) {
                const std::size_t base = (frame >> 1) * channels;
                const bool high_nibble = (frame & 1) != 0;
                for (std::size_t channel = 0; channel < channels; ++channel) {
                    decode_nibble(data[base + channel], high_nibble, state[channel]);
                    result.samples[frame * channels + channel] = static_cast<std::int16_t>(state[channel].predictor);
                }
            }
            return result;
        }
    } // namespace

    pcm_audio decode_godot_ima(const std::span<const std::byte> input, const std::uint32_t channels,
                               const std::uint32_t sample_rate, const audio_limits &limits)
    {
        return decode_ima(input, channels, sample_rate, std::nullopt, limits);
    }

    pcm_audio decode_godot_ima(const std::span<const std::byte> input, const std::uint32_t channels,
                               const std::uint32_t sample_rate, const std::uint64_t declared_frames,
                               const audio_limits &limits)
    {
        return decode_ima(input, channels, sample_rate, std::optional<std::uint64_t>(declared_frames), limits);
    }
} // namespace unpackrat::codecs
