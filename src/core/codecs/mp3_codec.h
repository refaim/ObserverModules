#pragma once

#include "audio_types.h"

#include "../io/byte_source.h"

#include <cstddef>
#include <span>

namespace unpackrat::codecs
{
    // Metadata reported for a recognized Godot ``AudioStreamMP3`` payload. The adapter exposes only
    // its own values: the vendored dr_mp3 types and error codes never leave the implementation.
    struct mp3_stream_info
    {
        std::uint32_t sample_rate = 0;
        std::uint32_t channels = 0;
        std::size_t frame_bytes = 0; // exact size of the first complete MPEG frame
    };

    // Recognize an ``AudioStreamMP3`` payload without decoding PCM: an optional ID3v2 tag followed
    // by a complete, bounded MPEG Layer III frame. Raises ``audio_error`` when the input is empty,
    // over the configured input budget, carries a malformed or oversized ID3v2 tag, is not a
    // complete frame, or is not Layer III. The vendored dr_mp3 decoder supplies the frame metadata
    // (sample rate, channel count, layer and exact frame size) with PCM synthesis skipped, so this
    // is safe to run from the descriptor probe.
    [[nodiscard]] mp3_stream_info probe_mp3(std::span<const std::byte> input, const audio_limits &limits = {});

    // Apply the same first-frame validation to a logical source without materializing a large
    // ID3 tag or MP3 payload. The reader is called only for the tag header and a decoder-derived
    // bounded frame window; caller budgets still apply to the complete logical source.
    [[nodiscard]] mp3_stream_info probe_mp3_source(const io::byte_source &source, const audio_limits &limits = {});

    // Decode a self-contained MP3 bitstream into interleaved PCM16. Only mono and stereo streams
    // are produced, matching the Godot sample phase. Input that is empty, over the configured
    // budget, or that the reference decoder rejects outright raises ``audio_error``. A stream the
    // decoder accepts is decoded exactly as the reference library reads it, including any leading
    // frames it cannot fully recover, so the adapter never promises more validation than dr_mp3
    // performs.
    [[nodiscard]] pcm_audio decode_mp3(std::span<const std::byte> input, const audio_limits &limits = {});
} // namespace unpackrat::codecs
