#ifndef UNPACKRAT_CORE_GODOT_AUDIO_RESOURCE_H
#define UNPACKRAT_CORE_GODOT_AUDIO_RESOURCE_H

#include "binary_resource.h"

#include "../codecs/audio_types.h"
#include "../io/byte_source.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <span>
#include <string_view>

// Converts the audio resources of a Godot binary resource into the container a desktop player
// understands, reusing the project's audio adapters rather than decoding or framing anything here:
//   * AudioStreamSample (Godot 3) / AudioStreamWAV (Godot 4) / Sample (Godot 2) -> PCM16 RIFF/WAVE
//   * AudioStreamMP3 -> the embedded MP3 bitstream, byte for byte
//   * AudioStreamOGGVorbis (Godot 3 raw bytes) -> the embedded Ogg stream, byte for byte
//   * AudioStreamOggVorbis (Godot 4) -> an Ogg stream remuxed from its OggPacketSequence
//
// Detection is descriptor-only. It inspects the root record's class and typed properties and never
// decodes a sample; the sample classes are decoded (through the codec adapters) only by
// ``write_audio_resource``. A resource whose class, layout or variant is not one of the supported
// forms resolves to no extension; a resource of a known class whose descriptor is structurally
// malformed raises ``codecs::audio_error``. Metadata inspection uses the stored length and a
// bounded capture prefix; later corruption in a media payload is reported during extraction.
namespace unpackrat::godot
{
    // Conservative class-only admission for a root binary resource; descriptor and engine checks
    // remain in the inspector.
    [[nodiscard]] bool supports_audio_resource_class(std::string_view class_name) noexcept;

    // Exact sizes are obtained from descriptor preflight for WAVE and embedded streams.
    // A packet sequence has no output size until extraction remuxes its packets.
    struct audio_resource_info final
    {
        std::string_view extension{};
        std::optional<std::uint64_t> output_size{};
    };

    [[nodiscard]] std::optional<audio_resource_info> inspect_audio_resource(const binary_resource &resource,
                                                                            const codecs::audio_limits &limits = {});

    // Classifies a descriptor read in metadata mode: byte vectors carry only a bounded prefix,
    // while byte_length records the complete declared payload. No payload decode or mux occurs.
    [[nodiscard]] std::optional<audio_resource_info> inspect_audio_resource_metadata(
        const binary_resource &resource, const codecs::audio_limits &limits = {});

    // The extension ("wav", "mp3" or "ogg") a supported resource would convert to, or nullopt when
    // the resource is not one of the supported audio streams. Validates the descriptor and the
    // container's capture header only; it never decodes or writes sample data.
    [[nodiscard]] std::optional<std::string_view> audio_resource_extension(const binary_resource &resource,
                                                                           const codecs::audio_limits &limits = {});

    // Converts ``resource`` to its canonical container, forwarding every produced byte range to
    // ``sink`` in order. The sink is borrowed for the duration of the call and an exception it
    // raises propagates unchanged to the caller. A malformed known resource and an unsupported one
    // both raise ``codecs::audio_error``.
    void write_audio_resource(const binary_resource &resource,
                              const std::function<void(std::span<const std::byte>)> &sink,
                              const codecs::audio_limits &limits = {});

    // Extract from a metadata-only descriptor and a borrowed logical RSRC/RSCC source. The
    // parser-provided byte offsets/lengths address the logical source; media is fetched in bounded
    // chunks and converted as it reaches the sink.
    void write_audio_resource_streaming(const binary_resource &metadata, const io::byte_source &source,
                                        const io::byte_sink &sink, const codecs::audio_limits &limits = {});
} // namespace unpackrat::godot
#endif
