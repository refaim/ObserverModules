#pragma once

// Converts a Godot Image or ImageTexture binary resource into a standalone image stream.
//
// The converter covers the two places an image is stored in a binary resource: the deprecated
// Godot 2 embedded Image variant (a raw pixel payload or a preserved "PNG "/"WEBP"-wrapped
// stream) and the Godot 3/4 Image resource, reached either from an ImageTexture's internal
// "image" reference or from a root Image record, whose "data" dictionary holds the raw pixel
// payload. Raw payloads are decoded to 8-bit RGBA and re-encoded as PNG; a preserved encoded
// payload is emitted byte for byte.
//
// This is a platform-neutral adapter: it depends only on the first-party binary-resource reader
// and the accepted 8-bit codecs, and it exposes its own value types and the shared
// ``codecs::image_error``. It never follows an external resource path, recurses into an arbitrary
// graph, or caches anything.

#include "core/codecs/image_types.h"
#include "core/godot/binary_resource.h"
#include "core/io/byte_source.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <span>
#include <string_view>

namespace unpackrat::godot
{
    // Conservative class-only admission. Engine-specific validity remains in inspection.
    [[nodiscard]] bool supports_image_resource_class(std::string_view class_name) noexcept;

    // Inspection never decodes pixels. Preserved streams report their logical byte length;
    // raw images report the representable RGBA extent streamed during extraction.
    struct image_resource_info final
    {
        std::string_view extension{};
        std::optional<std::uint64_t> output_size{};
        std::uint64_t decode_bytes = 0;
    };

    [[nodiscard]] std::optional<image_resource_info> inspect_image_resource(const binary_resource &resource,
                                                                            const codecs::image_limits &limits = {});

    // Inspects a metadata parse whose media vectors contain only bounded prefixes. Declared
    // byte_length fields validate image geometry and preserved-container extents without
    // requiring or decoding the skipped media bytes.
    [[nodiscard]] std::optional<image_resource_info> inspect_image_resource_metadata(
        const binary_resource &resource, const codecs::image_limits &limits = {});

    // True when ``write_raw_image`` can convert a base mip of ``wire_format`` for the engine
    // generation ``engine_major``. ``wire_format`` is the raw Image::Format id for Godot 3/4 and
    // the IMAGE_FORMAT id carried by the Godot 2 embedded Image variant.
    [[nodiscard]] bool supports_raw_image(std::uint32_t engine_major, std::uint32_t wire_format) noexcept;

    // Decodes the single base mip ``top_mip`` (engine_major/wire_format, width x height pixels) to
    // 8-bit RGBA and streams it to ``sink`` as a PNG. ``top_mip`` must hold exactly the base level
    // of the format, without any following mip levels. ``sink`` is called zero or more times with
    // successive pieces of the encoded stream; its own exception propagates unchanged. An
    // unsupported format, a malformed or wrongly sized payload, a breached limit or a checked
    // size overflow throws ``codecs::image_error``.
    void write_raw_image(std::span<const std::byte> top_mip, std::uint32_t engine_major, std::uint32_t wire_format,
                         std::uint32_t width, std::uint32_t height,
                         const std::function<void(std::span<const std::byte>)> &sink,
                         const codecs::image_limits &limits = {});

    // Structurally validates ``resource`` and returns the file extension of the stream
    // ``write_image_resource`` would produce, including the leading dot: ".png" for a freshly
    // encoded PNG and ".webp" for a preserved WebP stream. A resource that is not an image, or
    // whose class, reference shape or pixel format is unsupported, yields no value. A malformed
    // image - a missing or wrongly typed field, a non-positive dimension, an impossible mip chain,
    // a breached limit or a corrupt preserved envelope - throws ``codecs::image_error``. The probe
    // performs the same validation as the writer without decoding pixels or emitting anything.
    [[nodiscard]] std::optional<std::string_view> image_resource_extension(const binary_resource &resource,
                                                                           const codecs::image_limits &limits = {});

    // Resolves ``resource`` with the same validation as ``image_resource_extension`` and streams
    // the converted image to ``sink``. A preserved encoded payload is emitted unchanged; a raw
    // payload is decoded and written as PNG. A resource the probe reports as unsupported, and any
    // malformed image, throws ``codecs::image_error`` without emitting a stream.
    void write_image_resource(const binary_resource &resource,
                              const std::function<void(std::span<const std::byte>)> &sink,
                              const codecs::image_limits &limits = {});
    // Reads only one source scanline or one compressed four-row strip at a time. The PNG
    // encoder consumes successive RGBA scanlines; no whole-image source or RGBA buffer is built.
    void write_raw_image_streaming(const io::byte_source &top_mip, std::uint32_t engine_major,
                                   std::uint32_t wire_format, std::uint32_t width, std::uint32_t height,
                                   const io::byte_sink &sink, const codecs::image_limits &limits = {});
    // Resolves metadata byte offsets in logical and streams the base mip or preserved image.
    // Media extents are checked before any output; source/sink exceptions propagate unchanged.
    void write_image_resource_streaming(const binary_resource &metadata, const io::byte_source &logical,
                                        const io::byte_sink &sink, const codecs::image_limits &limits = {});
} // namespace unpackrat::godot
