#pragma once

// Physical-resource dispatcher for the Godot module.
//
// This is the adapter that turns one stored physical resource window into a converted view the
// desktop can open. It recognises the three container families the first-party readers understand
// through the accepted readers and writers:
//   * a texture envelope (Godot 3 "GDST", Godot 4 "GST2") probed through ``probe_texture`` and
//     either preserved byte for byte (an encoded PNG/WebP level) or decoded to PNG;
//   * a binary "RSRC" resource classified by its metadata and streamed through the accepted
//     image, audio or font writers;
//   * a block-compressed "RSCC" stream addressed by a bounded block decoder so extraction
//     can read logical payload ranges without materializing the full resource.
//
// Everything here is platform-neutral and value-oriented: it exposes only project-owned types plus
// the borrowed standard-stream/sink callback, and it depends only on the already accepted texture,
// binary-resource, RSCC and codec adapters.
//
// Probing declines unsupported binary root classes after their bounded header; supported classes
// continue metadata parsing without decoding or encoding media. Converted
// bytes are produced only by write_resource during extraction.

#include "core/godot/pck.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <istream>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>

namespace unpackrat::godot
{
    // The convertibility class of a physically recognised resource.
    enum class resource_kind : std::uint8_t
    {
        texture,
        image,
        audio,
        font,
    };

    // The descriptor ``probe_resource`` returns: the class and the leading-dot extension of the
    // stream ``write_resource`` would produce.
    struct resource_descriptor final
    {
        resource_kind kind = resource_kind::texture;
        std::string extension;
    };

    // A source-stream fault kept distinct from a declined but healthy candidate: the window could
    // not be seeked or delivered (a short physical stream, a hostile offset or an IO failure). It
    // is raised only for genuine source faults, never for a malformed but recognisable container.
    class resource_source_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Probes one stored physical resource window and returns its convertible descriptor, or no value
    // when the window is not a recognised convertible resource. An unrecognised byte string
    // or a malformed or unsupported container declines with no value; a genuine source fault throws
    // ``resource_source_error``.
    [[nodiscard]] std::optional<resource_descriptor> probe_resource(std::istream &stream, byte_range source);

    // Re-reads ``source`` from a fresh stream and streams the converted view described by
    // ``descriptor`` to ``sink``. A source fault throws ``resource_source_error`` and the sink's own
    // exception propagates unchanged.
    void write_resource(std::istream &stream, byte_range source, const resource_descriptor &descriptor,
                        const std::function<void(std::span<const std::byte>)> &sink);
} // namespace unpackrat::godot
