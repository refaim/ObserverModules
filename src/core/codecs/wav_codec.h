#ifndef UNPACKRAT_CORE_CODECS_WAV_CODEC_H
#define UNPACKRAT_CORE_CODECS_WAV_CODEC_H

#include "audio_types.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>

namespace unpackrat::codecs
{
    // Serialize ``audio`` as a PCM16 RIFF/WAVE stream, forwarding every produced byte range to
    // ``sink`` in order. The stream is written sequentially against the exact known PCM frame
    // count, so no seek is required. The writer's stream callback records any exception ``sink``
    // raises and reports a short write to the library; ``write_wav`` rethrows that exact exception
    // once control returns to C++, so nothing ever unwinds through the C callback.
    void write_wav(const pcm_audio &audio, const std::function<void(std::span<const std::byte>)> &sink,
                   const audio_limits &limits = {});

    // Pull at most 2048 interleaved frames at a time into caller-owned storage and write a
    // sequential RIFF stream. The callback must fill every requested sample or throw.
    void write_wav_streaming(std::uint64_t frames, std::uint32_t channels, std::uint32_t sample_rate,
                             const std::function<void(std::uint64_t, std::span<std::int16_t>)> &pull,
                             const std::function<void(std::span<const std::byte>)> &sink,
                             const audio_limits &limits = {});
} // namespace unpackrat::codecs
#endif
