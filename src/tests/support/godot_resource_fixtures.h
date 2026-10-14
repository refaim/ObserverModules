#ifndef UNPACKRAT_TESTS_SUPPORT_GODOT_RESOURCE_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_GODOT_RESOURCE_FIXTURES_H

// Repository-owned Godot binary-resource fixture builder. Every byte is generated here
// from the official wire layout; no external archive or engine artefact is read. The
// builders expose the raw header fields, the string/external/internal tables and the
// per-record properties so both well-formed documents and deliberately malformed ones can
// be produced without ad-hoc byte surgery in each test.

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace test::support
{
    using godot_resource_bytes = std::vector<std::uint8_t>;

    // The official Variant wire ids used by the fixtures. Legacy (Godot 2/3) and modern
    // (Godot 4) agree on the shared ids; the ids that differ are named per generation.
    inline constexpr std::uint32_t godot_tag_nil = 1;
    inline constexpr std::uint32_t godot_tag_bool = 2;
    inline constexpr std::uint32_t godot_tag_int = 3;
    inline constexpr std::uint32_t godot_tag_real = 4;
    inline constexpr std::uint32_t godot_tag_string = 5;
    inline constexpr std::uint32_t godot_tag_vector2 = 10;
    inline constexpr std::uint32_t godot_tag_rect2 = 11;
    inline constexpr std::uint32_t godot_tag_color = 20;
    inline constexpr std::uint32_t godot_tag_image = 21;
    inline constexpr std::uint32_t godot_tag_node_path = 22;
    inline constexpr std::uint32_t godot_tag_rid = 23;
    inline constexpr std::uint32_t godot_tag_object = 24;
    inline constexpr std::uint32_t godot_tag_input_event = 25;
    inline constexpr std::uint32_t godot_tag_dictionary = 26;
    inline constexpr std::uint32_t godot_tag_array = 30;
    inline constexpr std::uint32_t godot_tag_raw_array = 31;
    inline constexpr std::uint32_t godot_tag_int_array = 32;
    inline constexpr std::uint32_t godot_tag_real_array = 33;
    inline constexpr std::uint32_t godot_tag_string_array = 34;
    inline constexpr std::uint32_t godot_tag_vector3_array = 35;
    inline constexpr std::uint32_t godot_tag_color_array = 36;
    inline constexpr std::uint32_t godot_tag_vector2_array = 37;
    inline constexpr std::uint32_t godot_tag_int64 = 40;
    inline constexpr std::uint32_t godot_tag_double = 41;
    inline constexpr std::uint32_t godot_tag_string_name = 44;
    inline constexpr std::uint32_t godot_tag_packed_int64_array = 48;
    inline constexpr std::uint32_t godot_tag_packed_float64_array = 49;
    inline constexpr std::uint32_t godot_tag_packed_vector4_array = 53;

    inline constexpr std::uint32_t godot_object_empty = 0;
    inline constexpr std::uint32_t godot_object_external_resource = 1;
    inline constexpr std::uint32_t godot_object_internal_resource = 2;
    inline constexpr std::uint32_t godot_object_external_resource_index = 3;

    // A tiny wire writer. ``big_endian``/``real64`` fix how multi-byte scalars and reals are
    // laid down so a fixture can exercise the reader's wire-interpreted widths.
    class wire_writer final
    {
      public:
        explicit wire_writer(bool big_endian = false, bool real64 = false);

        wire_writer &u8(std::uint8_t value);
        wire_writer &u16(std::uint16_t value);
        wire_writer &u32(std::uint32_t value);
        wire_writer &u64(std::uint64_t value);
        wire_writer &real(double value);
        wire_writer &float32(float value);
        wire_writer &double64(double value);
        wire_writer &raw(std::span<const std::uint8_t> bytes);
        // Appends the 4-byte alignment padding the official saver writes after a byte blob.
        wire_writer &pad4(std::uint64_t length);
        // Appends a length-prefixed UTF-8 string including its NUL terminator.
        wire_writer &unicode(std::string_view text);
        // Appends an inline (high-bit) UTF-8 string including its NUL terminator.
        wire_writer &inline_unicode(std::string_view text);

        void patch_u32(std::size_t offset, std::uint32_t value);
        void patch_u64(std::size_t offset, std::uint64_t value);

        [[nodiscard]] std::size_t size() const noexcept;
        [[nodiscard]] const godot_resource_bytes &bytes() const noexcept;
        [[nodiscard]] godot_resource_bytes take();

      private:
        godot_resource_bytes bytes_;
        bool big_endian_;
        bool real64_;
    };

    // Encodes one Variant at the writer's current position using the actual official ids.
    class variant_encoder final
    {
      public:
        variant_encoder(wire_writer &out, bool modern, bool real64);

        variant_encoder &nil();
        variant_encoder &boolean(bool value);
        variant_encoder &int32(std::int32_t value);
        variant_encoder &int64(std::int64_t value);
        variant_encoder &real_value(double value);
        variant_encoder &double_value(double value);
        variant_encoder &string(std::string_view value);
        variant_encoder &bytes(std::span<const std::uint8_t> value);
        variant_encoder &int32_array(std::span<const std::int32_t> values);
        variant_encoder &int64_array(std::span<const std::int64_t> values);
        // Tag 33: the legacy real array (real-width elements) and the modern
        // PackedFloat32Array (always 32-bit elements).
        variant_encoder &real_array(std::span<const double> values);
        variant_encoder &float64_array(std::span<const double> values);
        variant_encoder &string_array(std::span<const std::string> values);
        variant_encoder &array(std::span<const godot_resource_bytes> elements);
        variant_encoder &object_empty();
        variant_encoder &object_internal(std::uint32_t index);
        variant_encoder &object_external(std::string_view type, std::string_view path);
        variant_encoder &object_external_index(std::uint32_t index);
        variant_encoder &image_raw(std::uint32_t width, std::uint32_t height, std::uint32_t mipmaps,
                                   std::uint32_t format, std::span<const std::uint8_t> data);
        variant_encoder &image_encoded(std::uint32_t encoding, std::span<const std::uint8_t> data);
        variant_encoder &vector2(double x, double y);
        variant_encoder &rid(std::uint32_t id);
        // Tag 25: the deprecated Godot 2 InputEvent, which carries no payload.
        variant_encoder &input_event();
        // Tag 22: a NodePath. ``names`` and ``subnames`` are string references (a table
        // index, or an inline id for Godot 3/4); ``extra_property`` writes the trailing
        // property reference the pre-format-3 layout always carries.
        variant_encoder &node_path(std::span<const std::uint32_t> names, std::span<const std::uint32_t> subnames,
                                   bool absolute, bool extra_property);
        // Tag 44: a string reference carrying an inline unicode string.
        variant_encoder &string_name(std::string_view value);
        // Tags 35/37: packed vector arrays with real-width elements (count * 3 / count * 2).
        variant_encoder &packed_vector3_array(std::uint32_t count);
        variant_encoder &packed_vector2_array(std::uint32_t count);
        // Tag 36: a packed colour array whose elements are always four 32-bit floats.
        variant_encoder &packed_color_array(std::uint32_t count);
        // ``keys_and_values`` alternates key, value, key, value ... for a dictionary.
        variant_encoder &dictionary(std::span<const godot_resource_bytes> keys_and_values);
        // Encodes a dictionary whose keys are inline unicode strings and whose values are the
        // supplied pre-encoded Variants, the string-keyed form the reader represents. When
        // ``shared`` is set the wire count carries the shared high bit.
        variant_encoder &string_dictionary(std::span<const std::pair<std::string, godot_resource_bytes>> entries,
                                           bool shared = false);
        // Writes a bare tag with no payload, for unknown-tag and malformed-tag tests.
        variant_encoder &raw_tag(std::uint32_t tag);

      private:
        wire_writer &out_;
        bool modern_;
        bool real64_;
    };

    struct resource_property_spec final
    {
        std::string name;
        godot_resource_bytes value;
        // Forces a literal name field (an index, or an inline id) instead of the resolved
        // encoding of ``name``; used to exercise out-of-range and inline name fields.
        std::optional<std::uint32_t> name_field;
    };

    struct resource_record_spec final
    {
        std::string path;
        std::string type;
        std::vector<resource_property_spec> properties;
        // Forces a literal property count instead of properties.size().
        std::optional<std::uint32_t> property_count;
    };

    struct resource_document_spec final
    {
        bool big_endian = false;
        bool use_real64 = false;  // legacy real width
        bool real64_flag = false; // modern FORMAT_FLAG_REAL_T_IS_DOUBLE
        std::uint32_t engine_major = 4;
        std::uint32_t engine_minor = 5;
        std::uint32_t format_version = 6;
        std::uint32_t format_flags = 0;
        bool include_magic = true;
        std::string main_type = "Resource";
        std::vector<std::string> string_table;
        std::vector<std::pair<std::string, std::string>> external_resources;
        std::vector<resource_record_spec> records;
        bool named_scene_ids = false; // modern FORMAT_FLAG_NAMED_SCENE_IDS
        bool modern_external_uids = false;
        bool has_script_class = false;
        std::string script_class;
        std::optional<std::uint64_t> import_offset;
        std::optional<std::uint32_t> string_table_count;
        std::optional<std::uint32_t> external_count;
        std::optional<std::uint32_t> internal_count;
        // Forces a literal internal-resource offset (all records) instead of the computed
        // one, to exercise out-of-range offset handling.
        std::optional<std::uint64_t> internal_offset;
    };

    [[nodiscard]] godot_resource_bytes build_resource(const resource_document_spec &spec);
} // namespace test::support
#endif
