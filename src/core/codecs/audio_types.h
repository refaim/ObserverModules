#ifndef UNPACKRAT_CORE_CODECS_AUDIO_TYPES_H
#define UNPACKRAT_CORE_CODECS_AUDIO_TYPES_H

#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

// Platform-neutral value types shared by every audio codec adapter. Nothing here depends on a
// third-party header: external types, error codes and decoder state ownership stay inside each
// adapter's implementation file.
namespace unpackrat::codecs
{
    // Decoded, interleaved 16-bit PCM. ``samples.size()`` is always a whole number of
    // ``channels``-wide frames; ``channels`` is one or two for the Godot phase.
    struct pcm_audio
    {
        std::uint32_t sample_rate = 0;
        std::uint32_t channels = 0;
        std::vector<std::int16_t> samples;
    };

    // Caller-specified bounds every adapter enforces before it allocates or writes. By default,
    // only addressability and format geometry bound a stream; no arbitrary media-size ceiling
    // may hide a valid archive entry.
    struct audio_limits
    {
        std::size_t max_input_bytes = (std::numeric_limits<std::size_t>::max)();
        std::size_t max_pcm_frames = (std::numeric_limits<std::size_t>::max)();
        std::size_t max_output_bytes = (std::numeric_limits<std::size_t>::max)();
    };

    class audio_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };
} // namespace unpackrat::codecs
#endif
