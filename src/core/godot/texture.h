#ifndef UNPACKRAT_CORE_GODOT_TEXTURE_H
#define UNPACKRAT_CORE_GODOT_TEXTURE_H

// Platform-neutral descriptor prober for the Godot texture container envelopes written by
// StreamTexture (Godot 3, "GDST") and CompressedTexture2D (Godot 4, "GST2"). It validates
// the fixed envelope, selects the top mip level and reports its ABSOLUTE byte range inside
// the caller's source window. It never decodes, re-encodes or allocates image data: only the
// fixed headers and a small encoded signature are read, so listing/probing a texture does not
// consume the encoded payload.
//
// Adapted from the canonical Godot texture readers, distributed under the MIT license:
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
// Sources pinned for traceability (commit, path):
//   godotengine/godot @ de2f0f147c5b7eff2d0f6dbc35042a4173fd59be (3.6-stable)
//     scene/resources/texture.{h,cpp}: StreamTexture::FormatBits, the 20-byte GDST header and
//       the PNG/WebP length-prefixed mip table.
//     modules/webp/image_loader_webp.cpp: the 4-byte "WEBP" prefix the 3.x packer writes in
//       front of the standard RIFF container.
//     core/image.h: the Image::Format ordinals.
//   godotengine/godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d (4.5-stable)
//     scene/resources/compressed_texture.{h,cpp}: the 36-byte GST2 header, the 16-byte
//       ImageMetadata block, DataFormat/FormatBits and the mipmaps+1 length-prefixed blocks.
//     modules/webp/webp_common.cpp: Godot 4 stores a plain RIFF/WebP payload (no prefix).
//     core/io/image.h: the Image::Format ordinals.
// Only the verified wire semantics are reused; the prober is re-implemented over the bounded
// core stream with explicit bounds and a strict "reject, never warn-and-continue" policy.

#include "pck.h"

#include <cstdint>
#include <istream>
#include <limits>
#include <stdexcept>

namespace unpackrat::godot
{
    // How the selected top mip level is carried on the wire.
    enum class texture_encoding : std::uint8_t
    {
        png,
        webp,
        raw,
    };

    // A validated descriptor of one texture container. ``body`` is the ABSOLUTE byte range of
    // the selected top mip level within the caller's stream: for an encoded payload the
    // preserved PNG bytes or the standard RIFF/WebP bytes (the Godot 3 "WEBP" wrapper removed),
    // and for a raw family the stored luminance/colour/block-compressed block. ``width`` and
    // ``height`` are the stored pixel dimensions (a GDST display/custom size never rescales
    // them). ``wire_image_format`` is the Godot Image::Format ordinal carried on the wire.
    struct texture_payload final
    {
        texture_encoding encoding = texture_encoding::raw;
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::uint32_t wire_image_format = 0;
        std::uint32_t engine_major = 0;
        byte_range body{};
    };

    // Optional caller ceilings enforced before seeking or reading. Defaults preserve every
    // physically bounded, representable texture; no image-size policy is imposed here.
    struct texture_limits final
    {
        std::uint64_t max_source_bytes = (std::numeric_limits<std::uint64_t>::max)();
        std::uint64_t max_pixels = (std::numeric_limits<std::uint64_t>::max)();
        std::uint32_t max_mipmaps = 32;
    };

    // Malformed, truncated, over-budget or otherwise invalid envelopes read from a healthy
    // source stream. This is the ordinary "declined conversion" failure.
    class texture_error : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // A source-stream fault, kept distinct from the envelope errors above so a caller can map it
    // to a source read failure instead of a decoded-but-rejected container. It is raised when the
    // stream could not seek or deliver the bytes the prober asked for: the declared window reaches
    // past the physical stream size, or a seek/read on the stream failed. It carries only its own
    // message (no vendor or Win32 error is stored) and stays catch-compatible with texture_error
    // by inheritance, so existing base-type handling keeps working unchanged.
    class texture_source_error final : public texture_error
    {
      public:
        using texture_error::texture_error;
    };

    // A structurally valid envelope carrying a recognized but unmodelled pixel family (Basis
    // Universal, ASTC, ETC, PVRTC, BC6H/HDR and other ordinals outside the supported set). The
    // caller can keep the compiled original instead of failing the whole container.
    class texture_unsupported final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Probes the texture container inside ``source``, an absolute window of ``stream``, and
    // returns the validated descriptor. Only the fixed envelope headers and a small encoded
    // signature are read; the encoded payload is never consumed. A malformed input within a
    // healthy source window throws texture_error, a source-stream seek/read fault (or a window
    // that reaches past the physical stream) throws the texture_source_error subtype, and a
    // recognized unsupported family throws texture_unsupported. The source-fault subtype exists
    // so the caller can map it to a source read error while still catching it as texture_error.
    [[nodiscard]] texture_payload probe_texture(std::istream &stream, byte_range source,
                                                const texture_limits &limits = {});
} // namespace unpackrat::godot
#endif
