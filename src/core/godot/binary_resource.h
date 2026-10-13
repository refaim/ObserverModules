#pragma once

// Platform-neutral reader for Godot binary resources: the "RSRC" container and the
// uncompressed logical payload of a compressed "RSCC" container (see read_binary_resource
// for the has_magic contract). It covers the header, string table, external and internal
// resource directories and the per-record property trees needed by ImageTexture,
// AudioStreamSample/AudioStreamWAV, AudioStreamMP3, AudioStreamOGGVorbis and
// OggPacketSequence. It never executes scripts, loads external resource paths or
// instantiates engine classes; a resource reference is preserved as a validated index.
//
// The reader is value-oriented and standard-library only: it depends on nothing but its
// own bounded byte span, never Observer, Win32 or the STL across the module C ABI.
//
// Adapted from the canonical Godot binary resource loader, distributed under the MIT
// license:
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
// Sources pinned for traceability (commit, path):
//   godotengine/godot @ 7249992b62bbe8ba01df41df1bb1b6122d1f2ba4 (2.1.6-stable)
//     core/io/resource_format_binary.{h,cpp}: legacy header, Variant wire ids and the
//     embedded Image variant.
//   godotengine/godot @ de2f0f147c5b7eff2d0f6dbc35042a4173fd59be (3.6-stable)
//     core/io/resource_format_binary.{h,cpp}: legacy header, int64/double and the
//     deprecated Image variant with a versioned image format field.
//   godotengine/godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d (4.5-stable)
//     core/io/resource_format_binary.{h,cpp}: modern header with flags/UID and the
//     Packed*Array variants.
//   godotengine/godot @ 232e6b14abf6e590b41d60f11704e42de22bff72 (master, 2026-10-09)
//     core/io/resource_format_binary.{h,cpp}: modern header, format version 6.
// Only the verified wire semantics are reused; the reader is re-implemented over a
// bounded span with checked counts, an explicit allocation/node/depth budget and a strict
// "reject, never warn-and-continue" policy.

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace unpackrat::godot
{
    // Ceilings the reader enforces before it allocates, seeks or recurses. Every default
    // bounds the whole logical stream; a caller may tighten any of them.
    struct resource_limits final
    {
        std::uint64_t max_input_bytes = 256ULL * 1024ULL * 1024ULL;
        std::uint64_t max_allocation_bytes = 256ULL * 1024ULL * 1024ULL;
        std::uint64_t max_nodes = 65536;
        // Bounds the stored length field of one string, including its NUL terminator.
        std::uint64_t max_string_bytes = 4ULL * 1024ULL * 1024ULL;
        std::uint32_t max_depth = 16;
    };

    class resource_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // A validated reference to another resource. The reader never follows a reference, so a
    // cycle in the wire data can never drive the reader; it only checks that an index is
    // within the directory it names and preserves the reference verbatim.
    struct resource_reference final
    {
        bool external = false; // false: index into the internal records; true: external table
        std::uint32_t index = 0;
        // OBJECT_EMPTY: an explicitly null object reference.
        bool empty = false;
        // The legacy inline external form carries its own type and path instead of an index.
        bool inline_definition = false;
        // Legacy object addressing: Godot 2/3, and legacy Godot 4, carry a subresource
        // identifier ("local://<id>") rather than a positional directory index. When set,
        // ``index`` is the resolved directory index and ``legacy_id`` preserves the wire id.
        bool has_legacy_id = false;
        std::uint32_t legacy_id = 0;
        std::string type;
        std::string path;
    };

    // The Godot 2/3 embedded Image variant payload. ``encoding`` is the raw wire marker
    // (1 raw, 2 lossless, 3 lossy); a raw payload carries dimensions and a pixel format,
    // an encoded payload carries only opaque image bytes.
    struct embedded_image final
    {
        std::uint32_t encoding = 0;
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::uint32_t mipmaps = 0;
        std::uint32_t format = 0;
        std::vector<std::byte> data;
        std::uint64_t byte_offset = 0;
        std::uint64_t byte_length = 0;
    };

    // The decoded value kinds. ``integer`` folds the 32- and 64-bit wire integers and
    // ``real`` folds the 32- and 64-bit wire reals into the widest lossless field;
    // ``integer_array`` and ``real_array`` fold the matching packed arrays the same way.
    // ``dictionary`` marks a dictionary whose keys are all strings; a structurally valid
    // dictionary with any non-string key stays ``unsupported``. ``unsupported`` also marks
    // the fixed-size math structures and their counted arrays.
    enum class value_kind : std::uint8_t
    {
        nil,
        boolean,
        integer,
        real,
        string,
        bytes,
        integer_array,
        real_array,
        array,
        dictionary,
        reference,
        embedded_image,
        unsupported,
    };

    // Forward declaration: a dictionary owns its entries, and each entry is a name plus a
    // value, so ``resource_value`` and ``resource_property`` are mutually recursive. The
    // element type may be incomplete here because it is completed below, before any member
    // of ``resource_value`` is used.
    struct resource_property;

    // A value-semantic node of the property tree. Only the fields named by ``kind`` are
    // meaningful; every other field keeps its default.
    struct resource_value final
    {
        value_kind kind = value_kind::nil;
        bool boolean = false;
        std::int64_t integer = 0;
        double real = 0.0;
        std::string string;
        std::vector<std::byte> bytes;
        std::uint64_t byte_offset = 0;
        std::uint64_t byte_length = 0;
        std::vector<std::int64_t> integer_array;
        std::vector<double> real_array;
        std::vector<resource_value> array;
        // One named entry per wire pair, in wire order. Only populated for ``dictionary``,
        // which requires every key to be a string; a duplicate name is rejected at read
        // time because it would make a media property ambiguous.
        std::vector<resource_property> dictionary;
        resource_reference reference;
        embedded_image image;
    };

    struct resource_property final
    {
        std::string name;
        resource_value value;
    };

    struct resource_record final
    {
        std::string path;
        std::string type;
        std::vector<resource_property> properties;
    };

    struct external_resource final
    {
        std::string type;
        std::string path;
    };

    struct internal_resource final
    {
        std::string path;
        std::uint64_t offset = 0;
    };

    struct engine_version final
    {
        std::uint32_t major = 0;
        std::uint32_t minor = 0;
    };

    struct binary_resource final
    {
        engine_version engine{};
        std::uint32_t format_version = 0;
        std::string main_type;
        std::vector<external_resource> external_resources;
        std::vector<internal_resource> internal_resources;
        // One record per internal resource, in directory order; the root is the last record.
        std::vector<resource_record> records;
    };

    // Reads a binary resource from ``data``.
    //
    // ``has_magic`` selects the two physical layouts the container appears in:
    //   * true  - ``data`` begins with the four-byte "RSRC" magic; every stored offset is a
    //             physical offset that already includes that magic.
    //   * false - ``data`` is the already-decompressed logical payload of an "RSCC" file,
    //             beginning at the endianness field; every stored offset is a logical offset
    //             measured from the first byte of ``data``.
    // The reader never prepends or shifts the magic: it interprets the stored offsets
    // directly in both modes. A compressed "RSCC" stream is not handled here; the future
    // compression wrapper decompresses first and calls this with has_magic = false.
    //
    // Malformed, truncated, over-budget or unmodelled input throws resource_error.
    [[nodiscard]] binary_resource read_binary_resource(std::span<const std::byte> data, bool has_magic = true,
                                                       const resource_limits &limits = {});

    // Parses structural metadata from a bounded random-access source. Byte arrays and embedded
    // images retain only a short prefix; byte_length records their full checked wire extent.
    [[nodiscard]] binary_resource read_binary_resource_metadata(
        std::uint64_t logical_size, const std::function<void(std::uint64_t, std::span<std::byte>)> &read_at,
        bool has_magic = true, const resource_limits &limits = {});

    // Validates the bounded header, then offers its main type to a caller-provided admission
    // predicate. A declined type returns nullopt without reading tables, records or media;
    // accepted types continue the same metadata parse. The string view is borrowed only
    // for the duration of the predicate call. Callback exceptions propagate unchanged.
    [[nodiscard]] std::optional<binary_resource> read_binary_resource_metadata_if(
        std::uint64_t logical_size, const std::function<void(std::uint64_t, std::span<std::byte>)> &read_at,
        const std::function<bool(std::string_view)> &accept_main_type, bool has_magic = true,
        const resource_limits &limits = {});
} // namespace unpackrat::godot
