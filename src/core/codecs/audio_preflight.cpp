#include "audio_preflight.h"

#include "audio_types.h"

#include <cstdint>
#include <limits>

namespace unpackrat::codecs::preflight
{
    namespace
    {
        // The largest payload a RIFF data chunk can describe: its 32-bit size field minus the
        // canonical WAVE + fmt + data-header bytes that a PCM16 stream carries alongside it.
        constexpr std::uint64_t riff_payload_limit = 0xFFFFFFFFULL - 36ULL;
    } // namespace

    std::size_t interleaved_sample_count(const std::uint64_t frames, const std::uint64_t channels)
    {
        constexpr std::uint64_t limit = std::numeric_limits<std::size_t>::max();
        if (frames != 0 && channels > limit / frames) {
            throw audio_error("audio: the interleaved sample count is not representable");
        }
        return static_cast<std::size_t>(frames * channels);
    }

    std::uint64_t ima_frame_capacity(const std::uint64_t packed_bytes, const std::uint32_t channels)
    {
        if (channels != 1 && channels != 2) {
            throw audio_error("godot-ima: only mono and stereo payloads are supported");
        }
        if (packed_bytes > (std::numeric_limits<std::uint64_t>::max)() / 2) {
            throw audio_error("godot-ima: encoded frame capacity overflows");
        }
        return packed_bytes * 2 / channels;
    }

    std::uint64_t riff_pcm16_stream_bytes(const std::uint64_t frames, const std::uint64_t channels)
    {
        // Only mono and stereo PCM16 streams are expressible in this phase. Pinning the lane count
        // before the multiply keeps ``channels * 2`` from overflowing a 64-bit input, so the payload
        // arithmetic below can never wrap.
        if (channels != 1 && channels != 2) {
            throw audio_error("wav: the PCM16 stream cannot be expressed in a RIFF container");
        }
        const std::uint64_t bytes_per_frame = channels * 2;
        if (frames > riff_payload_limit / bytes_per_frame) {
            throw audio_error("wav: the PCM16 stream cannot be expressed in a RIFF container");
        }
        // The payload is bounded by ``riff_payload_limit``, so the header addition stays well inside
        // 64 bits and the full format size may legitimately exceed a 32-bit ``std::size_t``.
        return frames * bytes_per_frame + 44;
    }
} // namespace unpackrat::codecs::preflight
