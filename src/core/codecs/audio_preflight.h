#ifndef UNPACKRAT_CORE_CODECS_AUDIO_PREFLIGHT_H
#define UNPACKRAT_CORE_CODECS_AUDIO_PREFLIGHT_H

#include <cstddef>
#include <cstdint>

// Adapter-internal numeric preflight shared by the audio codecs. These helpers hold the arithmetic
// that must be decided before an untrusted count reaches a third-party library or an allocation, so
// the size_t representability and RIFF container limits can be exercised directly on this host
// (and stay correct on a 32-bit build) without materializing gigabyte buffers. They are pure,
// allocation-free, and expose only project-owned values and the shared ``audio_error``.
namespace unpackrat::codecs::preflight
{
    // ``frames * channels`` interleaved samples, raising ``audio_error`` when the product is not
    // representable in ``std::size_t``. Callers derive the windowed frame or lane count from
    // untrusted header fields, so this must run before the product is used.
    [[nodiscard]] std::size_t interleaved_sample_count(std::uint64_t frames, std::uint64_t channels);

    // Logical frame capacity of a Godot IMA payload: two nibbles per source byte spread over
    // one or two channels. Metadata readers can validate this 64-bit count without materializing
    // the payload; invalid channel counts or doubling overflow raise ``audio_error``.
    [[nodiscard]] std::uint64_t ima_frame_capacity(std::uint64_t packed_bytes, std::uint32_t channels);

    // Byte size of a canonical PCM16 RIFF/WAVE stream carrying ``frames`` interleaved frames across
    // ``channels`` lanes: a 44-byte header plus ``frames * channels * 2`` payload bytes. This is a
    // pure format size, so it is returned as 64-bit and never narrowed here: the largest legal mono
    // stream is a little over 4 GiB and would wrap a 32-bit ``std::size_t``. Raises ``audio_error``
    // when that stream cannot be expressed, either because the payload passes the RIFF 32-bit chunk
    // limit (0xFFFFFFFF - 36) or because ``channels`` is not the mono/stereo geometry this format
    // supports; no caller budget can lift either limit.
    [[nodiscard]] std::uint64_t riff_pcm16_stream_bytes(std::uint64_t frames, std::uint64_t channels);
} // namespace unpackrat::codecs::preflight
#endif
