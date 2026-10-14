#include "binary_resource.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

// Adapted from the canonical Godot binary resource loader, distributed under the MIT
// license:
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
// Sources pinned for traceability (commit, path):
//   godotengine/godot @ 7249992b62bbe8ba01df41df1bb1b6122d1f2ba4 (2.1.6-stable)
//     core/io/resource_format_binary.cpp: header, string/external/internal tables,
//     parse_variant ids, the embedded Image variant and the string-table name index.
//   godotengine/godot @ de2f0f147c5b7eff2d0f6dbc35042a4173fd59be (3.6-stable)
//     core/io/resource_format_binary.cpp: legacy header, int64/double, the deprecated
//     Image variant with a versioned format field and the legacy object references.
//   godotengine/godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d (4.5-stable)
//     core/io/resource_format_binary.{h,cpp}: modern header with flags/UID/script class,
//     the inline (high-bit) name string, Packed*Array ids and the modern object forms.
//   godotengine/godot @ 232e6b14abf6e590b41d60f11704e42de22bff72 (master, 2026-10-09)
//     core/io/resource_format_binary.{h,cpp}: modern header, format version 6.
// Only the verified wire semantics are reused; the reader re-implements them over a bounded
// span with checked counts, an explicit allocation/node/depth budget and a strict
// "reject, never warn-and-continue" policy. The MIT notice text ships in licenses/.

namespace unpackrat::godot
{
    namespace
    {
        using byte = std::byte;

        // Official tag ids, shared by the legacy (Godot 2/3) and modern (Godot 4) tables.
        constexpr std::uint32_t tag_nil = 1;
        constexpr std::uint32_t tag_bool = 2;
        constexpr std::uint32_t tag_int = 3;
        constexpr std::uint32_t tag_real = 4;
        constexpr std::uint32_t tag_string = 5;
        constexpr std::uint32_t tag_vector2 = 10;
        constexpr std::uint32_t tag_rect2 = 11;
        constexpr std::uint32_t tag_vector3 = 12;
        constexpr std::uint32_t tag_plane = 13;
        constexpr std::uint32_t tag_quat = 14;
        constexpr std::uint32_t tag_aabb = 15;
        constexpr std::uint32_t tag_matrix = 16;
        constexpr std::uint32_t tag_transform = 17;
        constexpr std::uint32_t tag_transform2d = 18;
        constexpr std::uint32_t tag_color = 20;
        constexpr std::uint32_t tag_image = 21;
        constexpr std::uint32_t tag_node_path = 22;
        constexpr std::uint32_t tag_rid = 23;
        constexpr std::uint32_t tag_object = 24;
        constexpr std::uint32_t tag_input_event = 25;
        constexpr std::uint32_t tag_dictionary = 26;
        constexpr std::uint32_t tag_array = 30;
        constexpr std::uint32_t tag_raw_array = 31;
        constexpr std::uint32_t tag_int_array = 32;
        constexpr std::uint32_t tag_real_array = 33;
        constexpr std::uint32_t tag_string_array = 34;
        constexpr std::uint32_t tag_vector3_array = 35;
        constexpr std::uint32_t tag_color_array = 36;
        constexpr std::uint32_t tag_vector2_array = 37;
        constexpr std::uint32_t tag_int64 = 40;
        constexpr std::uint32_t tag_double = 41;
        constexpr std::uint32_t tag_callable = 42;
        constexpr std::uint32_t tag_signal = 43;
        constexpr std::uint32_t tag_string_name = 44;
        constexpr std::uint32_t tag_vector2i = 45;
        constexpr std::uint32_t tag_rect2i = 46;
        constexpr std::uint32_t tag_vector3i = 47;
        constexpr std::uint32_t tag_int64_array = 48;
        constexpr std::uint32_t tag_real64_array = 49;
        constexpr std::uint32_t tag_vector4 = 50;
        constexpr std::uint32_t tag_vector4i = 51;
        constexpr std::uint32_t tag_projection = 52;
        constexpr std::uint32_t tag_vector4_array = 53;

        constexpr std::uint32_t object_empty = 0;
        constexpr std::uint32_t object_external_resource = 1;
        constexpr std::uint32_t object_internal_resource = 2;
        constexpr std::uint32_t object_external_resource_index = 3;

        constexpr std::uint32_t format_flag_named_scene_ids = 1U << 0U;
        constexpr std::uint32_t format_flag_uids = 1U << 1U;
        constexpr std::uint32_t format_flag_real_is_double = 1U << 2U;
        constexpr std::uint32_t format_flag_has_script_class = 1U << 3U;

        // Fixed-skip unit codes: a real-width scalar or an always-four-byte scalar.
        constexpr std::uint32_t skip_unit_real = 0;
        constexpr std::uint32_t skip_unit_fixed = 1;

        constexpr std::uint32_t reserved_fields_legacy = 14;
        constexpr std::uint32_t reserved_fields_modern = 11;

        constexpr std::uint32_t max_format_legacy_v2 = 1;
        constexpr std::uint32_t max_format_legacy_v3 = 3;
        constexpr std::uint32_t max_format_modern = 6;
        // Format version at which the NodePath trailing property field was dropped.
        constexpr std::uint32_t format_version_no_nodepath_property = 3;
        constexpr std::uint32_t engine_major_legacy_v2 = 2;
        constexpr std::uint32_t engine_major_legacy_v3 = 3;
        constexpr std::uint32_t engine_major_modern = 4;

        [[noreturn]] void fail(const char *message)
        {
            throw resource_error(message);
        }

        class parser;
        // A decoder receives the depth it was entered at plus the entry's skip unit and
        // component count; decoders that carry no payload ignore the last two.
        using decoder_fn = resource_value (*)(parser &, std::uint32_t depth, std::uint32_t unit,
                                              std::uint32_t components);

        resource_value decode_nil(parser &, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_bool(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_int32(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_int64(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_real(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_double(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_string(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_bytes(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_int_array32(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_int_array64(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_real_array_width(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_real_array32(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_real_array64(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_string_array(parser &reader, std::uint32_t depth, std::uint32_t, std::uint32_t);
        resource_value decode_array(parser &reader, std::uint32_t depth, std::uint32_t, std::uint32_t);
        resource_value decode_object(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_image(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_node_path(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_string_name(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_input_event(parser &, std::uint32_t, std::uint32_t, std::uint32_t);
        resource_value decode_skip_fixed(parser &reader, std::uint32_t, const std::uint32_t unit,
                                         const std::uint32_t components);
        resource_value decode_skip_counted(parser &reader, std::uint32_t, const std::uint32_t unit,
                                           const std::uint32_t components);
        resource_value decode_dictionary(parser &reader, std::uint32_t depth, std::uint32_t, std::uint32_t);

        struct tag_entry final
        {
            std::uint32_t raw;
            decoder_fn decode;
            std::uint32_t unit;
            std::uint32_t components;
        };

        // A Godot 2/3 (or legacy Godot 4) internal resource identifier: the "local://<id>"
        // subresource id and the directory index it names.
        struct legacy_id_entry final
        {
            std::uint32_t id;
            std::uint32_t index;
        };

        // The legacy Godot 2 wire ids. Node paths are structurally skipped; the deprecated
        // zero-payload InputEvent exists only here. Every entry is data; the lookup adds no
        // per-entry branch.
        constexpr tag_entry legacy_v2_tags[] = {
            {tag_nil, decode_nil, 0, 0},
            {tag_bool, decode_bool, 0, 0},
            {tag_int, decode_int32, 0, 0},
            {tag_real, decode_real, 0, 0},
            {tag_string, decode_string, 0, 0},
            {tag_vector2, decode_skip_fixed, skip_unit_real, 2},
            {tag_rect2, decode_skip_fixed, skip_unit_real, 4},
            {tag_vector3, decode_skip_fixed, skip_unit_real, 3},
            {tag_plane, decode_skip_fixed, skip_unit_real, 4},
            {tag_quat, decode_skip_fixed, skip_unit_real, 4},
            {tag_aabb, decode_skip_fixed, skip_unit_real, 6},
            {tag_matrix, decode_skip_fixed, skip_unit_real, 9},
            {tag_transform, decode_skip_fixed, skip_unit_real, 12},
            {tag_transform2d, decode_skip_fixed, skip_unit_real, 6},
            {tag_color, decode_skip_fixed, skip_unit_real, 4},
            {tag_image, decode_image, 0, 0},
            {tag_node_path, decode_node_path, 0, 0},
            {tag_rid, decode_skip_fixed, skip_unit_fixed, 1},
            {tag_object, decode_object, 0, 0},
            {tag_input_event, decode_input_event, 0, 0},
            {tag_dictionary, decode_dictionary, 0, 0},
            {tag_array, decode_array, 0, 0},
            {tag_raw_array, decode_bytes, 0, 0},
            {tag_int_array, decode_int_array32, 0, 0},
            {tag_real_array, decode_real_array_width, 0, 0},
            {tag_string_array, decode_string_array, 0, 0},
            {tag_vector3_array, decode_skip_counted, skip_unit_real, 3},
            {tag_color_array, decode_skip_counted, skip_unit_real, 4},
            {tag_vector2_array, decode_skip_counted, skip_unit_real, 2},
            {tag_int64, decode_int64, 0, 0},
            {tag_double, decode_double, 0, 0},
        };

        // The legacy Godot 3 wire ids. Node paths are structurally skipped; InputEvent is
        // gone (its tag is rejected). Colours use the wire real width.
        constexpr tag_entry legacy_v3_tags[] = {
            {tag_nil, decode_nil, 0, 0},
            {tag_bool, decode_bool, 0, 0},
            {tag_int, decode_int32, 0, 0},
            {tag_real, decode_real, 0, 0},
            {tag_string, decode_string, 0, 0},
            {tag_vector2, decode_skip_fixed, skip_unit_real, 2},
            {tag_rect2, decode_skip_fixed, skip_unit_real, 4},
            {tag_vector3, decode_skip_fixed, skip_unit_real, 3},
            {tag_plane, decode_skip_fixed, skip_unit_real, 4},
            {tag_quat, decode_skip_fixed, skip_unit_real, 4},
            {tag_aabb, decode_skip_fixed, skip_unit_real, 6},
            {tag_matrix, decode_skip_fixed, skip_unit_real, 9},
            {tag_transform, decode_skip_fixed, skip_unit_real, 12},
            {tag_transform2d, decode_skip_fixed, skip_unit_real, 6},
            {tag_color, decode_skip_fixed, skip_unit_real, 4},
            {tag_image, decode_image, 0, 0},
            {tag_node_path, decode_node_path, 0, 0},
            {tag_rid, decode_skip_fixed, skip_unit_fixed, 1},
            {tag_object, decode_object, 0, 0},
            {tag_dictionary, decode_dictionary, 0, 0},
            {tag_array, decode_array, 0, 0},
            {tag_raw_array, decode_bytes, 0, 0},
            {tag_int_array, decode_int_array32, 0, 0},
            {tag_real_array, decode_real_array_width, 0, 0},
            {tag_string_array, decode_string_array, 0, 0},
            {tag_vector3_array, decode_skip_counted, skip_unit_real, 3},
            {tag_color_array, decode_skip_counted, skip_unit_real, 4},
            {tag_vector2_array, decode_skip_counted, skip_unit_real, 2},
            {tag_int64, decode_int64, 0, 0},
            {tag_double, decode_double, 0, 0},
        };

        // The modern (Godot 4) wire ids. Node paths and string names are structurally
        // skipped; InputEvent is gone. Colours and packed colour arrays are always 32-bit,
        // so their skip unit is the fixed four-byte scalar.
        constexpr tag_entry modern_tags[] = {
            {tag_nil, decode_nil, 0, 0},
            {tag_bool, decode_bool, 0, 0},
            {tag_int, decode_int32, 0, 0},
            {tag_real, decode_real, 0, 0},
            {tag_string, decode_string, 0, 0},
            {tag_vector2, decode_skip_fixed, skip_unit_real, 2},
            {tag_rect2, decode_skip_fixed, skip_unit_real, 4},
            {tag_vector3, decode_skip_fixed, skip_unit_real, 3},
            {tag_plane, decode_skip_fixed, skip_unit_real, 4},
            {tag_quat, decode_skip_fixed, skip_unit_real, 4},
            {tag_aabb, decode_skip_fixed, skip_unit_real, 6},
            {tag_matrix, decode_skip_fixed, skip_unit_real, 9},
            {tag_transform, decode_skip_fixed, skip_unit_real, 12},
            {tag_transform2d, decode_skip_fixed, skip_unit_real, 6},
            {tag_color, decode_skip_fixed, skip_unit_fixed, 4},
            {tag_node_path, decode_node_path, 0, 0},
            {tag_rid, decode_skip_fixed, skip_unit_fixed, 1},
            {tag_object, decode_object, 0, 0},
            {tag_dictionary, decode_dictionary, 0, 0},
            {tag_array, decode_array, 0, 0},
            {tag_raw_array, decode_bytes, 0, 0},
            {tag_int_array, decode_int_array32, 0, 0},
            {tag_real_array, decode_real_array32, 0, 0},
            {tag_string_array, decode_string_array, 0, 0},
            {tag_vector3_array, decode_skip_counted, skip_unit_real, 3},
            {tag_color_array, decode_skip_counted, skip_unit_fixed, 4},
            {tag_vector2_array, decode_skip_counted, skip_unit_real, 2},
            {tag_int64, decode_int64, 0, 0},
            {tag_double, decode_double, 0, 0},
            {tag_callable, decode_skip_fixed, skip_unit_fixed, 0},
            {tag_signal, decode_skip_fixed, skip_unit_fixed, 0},
            {tag_string_name, decode_string_name, 0, 0},
            {tag_vector2i, decode_skip_fixed, skip_unit_fixed, 2},
            {tag_rect2i, decode_skip_fixed, skip_unit_fixed, 4},
            {tag_vector3i, decode_skip_fixed, skip_unit_fixed, 3},
            {tag_int64_array, decode_int_array64, 0, 0},
            {tag_real64_array, decode_real_array64, 0, 0},
            {tag_vector4, decode_skip_fixed, skip_unit_real, 4},
            {tag_vector4i, decode_skip_fixed, skip_unit_fixed, 4},
            {tag_projection, decode_skip_fixed, skip_unit_real, 16},
            {tag_vector4_array, decode_skip_counted, skip_unit_real, 4},
        };

        // The reader. Every count, offset, string and raw array is bounded before the reader
        // allocates, seeks or recurses, and the aggregate allocation, node and depth budgets
        // are charged before the matching operation. All arithmetic is done in 64 bits and
        // the narrowing to size_t happens only after the bound is proven against the span.
        class parser final
        {
          public:
            parser(std::span<const byte> data, const bool has_magic, const resource_limits &limits)
                : data_(data), data_size_(data.size()), has_magic_(has_magic), limits_(&limits)
            {
            }

            parser(const std::uint64_t logical_size, const std::function<void(std::uint64_t, std::span<byte>)> &read_at,
                   const bool has_magic, const resource_limits &limits)
                : data_size_(logical_size), read_at_(&read_at), metadata_only_(true), has_magic_(has_magic),
                  limits_(&limits)
            {
            }

            binary_resource parse();
            std::optional<binary_resource> parse_if(const std::function<bool(std::string_view)> &accept_main_type);

            std::uint64_t remaining() const noexcept
            {
                return data_size_ - position_;
            }

            void require(const std::uint64_t count) const
            {
                if (count > remaining()) {
                    fail("resource data is truncated");
                }
            }

            void seek(const std::uint64_t target)
            {
                if (target > data_size_) {
                    fail("resource offset is outside the input");
                }
                position_ = target;
            }

            void skip(const std::uint64_t count)
            {
                require(count);
                position_ += count;
            }

            template <std::size_t Width> std::array<byte, Width> read_fixed(const std::uint64_t index) const
            {
                std::array<byte, Width> bytes{};
                if (read_at_ != nullptr) {
                    (*read_at_)(index, bytes);
                } else {
                    std::copy_n(data_.data() + static_cast<std::size_t>(index), Width, bytes.data());
                }
                return bytes;
            }

            bool metadata_only() const noexcept
            {
                return metadata_only_;
            }

            std::uint64_t position() const noexcept
            {
                return position_;
            }

            std::uint16_t u16();
            std::uint32_t u32();
            std::uint64_t u64();

            std::span<const byte> take(const std::uint64_t count)
            {
                require(count);
                std::span<const byte> span;
                if (read_at_ != nullptr) {
                    // In metadata mode callers fetch only bounded strings or media prefixes.
                    // A full media length is advanced with skip(), never passed to take().
                    if (count > limits_->max_string_bytes) {
                        fail("metadata fetch exceeds the string buffer bound");
                    }
                    scratch_.resize(static_cast<std::size_t>(count));
                    (*read_at_)(position_, scratch_);
                    span = scratch_;
                } else {
                    span = data_.subspan(static_cast<std::size_t>(position_), static_cast<std::size_t>(count));
                }
                position_ += count;
                return span;
            }

            float read_float32()
            {
                return std::bit_cast<float>(u32());
            }

            double read_double()
            {
                return std::bit_cast<double>(u64());
            }

            double read_real()
            {
                return real_is_double_ ? read_double() : static_cast<double>(read_float32());
            }

            std::uint64_t real_width() const noexcept
            {
                return real_is_double_ ? 8 : 4;
            }

            void charge(const std::uint64_t amount)
            {
                if (amount > limits_->max_allocation_bytes - allocation_) {
                    fail("resource exceeds the allocation budget");
                }
                allocation_ += amount;
            }

            void enter_node()
            {
                if (nodes_ >= limits_->max_nodes) {
                    fail("resource exceeds the value node budget");
                }
                ++nodes_;
            }

            void require_depth(const std::uint32_t depth) const
            {
                if (depth > limits_->max_depth) {
                    fail("resource value nesting exceeds the depth budget");
                }
            }

            std::uint32_t read_count(const std::uint64_t minimum_bytes)
            {
                const std::uint32_t count = u32();
                require(static_cast<std::uint64_t>(count) * minimum_bytes);
                return count;
            }

            std::uint32_t read_shared_count(const std::uint64_t minimum_bytes)
            {
                std::uint32_t count = u32();
                count &= 0x7FFFFFFFU;
                require(static_cast<std::uint64_t>(count) * minimum_bytes);
                return count;
            }

            void align4(const std::uint64_t length)
            {
                skip((4 - (length % 4)) % 4);
            }

            std::string read_string_body(const std::uint32_t length);
            std::string read_unicode_string();
            std::string read_name();

            const std::string &string_at(const std::uint32_t index) const
            {
                if (index >= strings_.size()) {
                    fail("resource string index is out of range");
                }
                return strings_[index];
            }

            void require_internal_index(const std::uint32_t index) const
            {
                if (index >= result_.internal_resources.size()) {
                    fail("internal resource reference is out of range");
                }
            }

            void require_external_index(const std::uint32_t index) const
            {
                if (index >= result_.external_resources.size()) {
                    fail("external resource reference is out of range");
                }
            }

            bool versioned_image_format() const noexcept
            {
                return versioned_image_format_;
            }

            std::uint32_t format_version() const noexcept
            {
                return format_version_;
            }

            // Godot 2/3 and legacy Godot 4 (no FORMAT_FLAG_NAMED_SCENE_IDS) address an
            // internal resource by its "local://<id>" subresource id rather than by a
            // positional directory index; resolve the wire id to the validated directory
            // index. A missing id rejects rather than guessing an offset.
            std::uint32_t resolve_internal_reference(const std::uint32_t id) const
            {
                if (!legacy_id_mode()) {
                    require_internal_index(id);
                    return id;
                }
                const auto found = std::lower_bound(
                    legacy_ids_.begin(), legacy_ids_.end(), id,
                    [](const legacy_id_entry &entry, const std::uint32_t value) { return entry.id < value; });
                if (found == legacy_ids_.end() || found->id != id) {
                    fail("internal resource reference id is unknown");
                }
                return found->index;
            }

            bool legacy_id_mode() const noexcept
            {
                return !modern_ || !named_scene_ids_;
            }

            // A child container's element count is checked against the node and depth
            // budgets before the element vector is reserved, so an impossible budget fails
            // before any input-driven allocation.
            void preflight_children(const std::uint64_t count, const std::uint32_t child_depth) const
            {
                if (count == 0) {
                    return;
                }
                require_depth(child_depth);
                if (count > limits_->max_nodes - nodes_) {
                    fail("resource exceeds the value node budget");
                }
            }

            resource_value read_value(const std::uint32_t depth);

          private:
            std::optional<tag_entry> find_tag(const std::uint32_t raw) const
            {
                const std::span<const tag_entry> table = modern_ ? std::span<const tag_entry>(modern_tags)
                                                                 : (v2_ ? std::span<const tag_entry>(legacy_v2_tags)
                                                                        : std::span<const tag_entry>(legacy_v3_tags));
                const auto found = std::find_if(table.begin(), table.end(),
                                                [raw](const tag_entry &entry) { return entry.raw == raw; });
                if (found == table.end()) {
                    return std::nullopt;
                }
                return *found;
            }

            void build_legacy_ids();
            void read_header();
            void read_tables();
            void read_records();

            std::span<const byte> data_;
            std::uint64_t data_size_ = 0;
            const std::function<void(std::uint64_t, std::span<byte>)> *read_at_ = nullptr;
            bool metadata_only_ = false;
            std::vector<byte> scratch_;
            std::uint64_t position_ = 0;
            bool has_magic_ = true;
            bool big_endian_ = false;
            bool real_is_double_ = false;
            bool modern_ = false;
            bool v2_ = false;
            bool using_uids_ = false;
            bool named_scene_ids_ = false;
            bool inline_names_ = true;
            bool versioned_image_format_ = false;
            std::uint32_t format_version_ = 0;
            const resource_limits *limits_ = nullptr;
            std::uint64_t allocation_ = 0;
            std::uint64_t nodes_ = 0;
            std::vector<std::string> strings_;
            std::vector<legacy_id_entry> legacy_ids_;
            binary_resource result_;
        };

        std::uint16_t parser::u16()
        {
            require(2);
            const auto bytes = read_fixed<2>(position_);
            const std::uint32_t low = std::to_integer<std::uint8_t>(bytes[0]);
            const std::uint32_t high = std::to_integer<std::uint8_t>(bytes[1]);
            position_ += 2;
            if (big_endian_) {
                return static_cast<std::uint16_t>((low << 8U) | high);
            }
            return static_cast<std::uint16_t>(low | (high << 8U));
        }

        std::uint32_t parser::u32()
        {
            require(4);
            const auto bytes = read_fixed<4>(position_);
            const std::uint32_t b0 = std::to_integer<std::uint8_t>(bytes[0]);
            const std::uint32_t b1 = std::to_integer<std::uint8_t>(bytes[1]);
            const std::uint32_t b2 = std::to_integer<std::uint8_t>(bytes[2]);
            const std::uint32_t b3 = std::to_integer<std::uint8_t>(bytes[3]);
            position_ += 4;
            if (big_endian_) {
                return (b0 << 24U) | (b1 << 16U) | (b2 << 8U) | b3;
            }
            return b0 | (b1 << 8U) | (b2 << 16U) | (b3 << 24U);
        }

        std::uint64_t parser::u64()
        {
            require(8);
            const auto bytes = read_fixed<8>(position_);
            const std::uint64_t b0 = std::to_integer<std::uint8_t>(bytes[0]);
            const std::uint64_t b1 = std::to_integer<std::uint8_t>(bytes[1]);
            const std::uint64_t b2 = std::to_integer<std::uint8_t>(bytes[2]);
            const std::uint64_t b3 = std::to_integer<std::uint8_t>(bytes[3]);
            const std::uint64_t b4 = std::to_integer<std::uint8_t>(bytes[4]);
            const std::uint64_t b5 = std::to_integer<std::uint8_t>(bytes[5]);
            const std::uint64_t b6 = std::to_integer<std::uint8_t>(bytes[6]);
            const std::uint64_t b7 = std::to_integer<std::uint8_t>(bytes[7]);
            position_ += 8;
            if (big_endian_) {
                return (b0 << 56U) | (b1 << 48U) | (b2 << 40U) | (b3 << 32U) | (b4 << 24U) | (b5 << 16U) | (b6 << 8U) |
                       b7;
            }
            return b0 | (b1 << 8U) | (b2 << 16U) | (b3 << 24U) | (b4 << 32U) | (b5 << 40U) | (b6 << 48U) | (b7 << 56U);
        }

        // Validates that ``bytes`` is well-formed UTF-8. An overlong encoding, a surrogate, a
        // code point above U+10FFFF, a bad lead byte, a bad continuation byte or a truncated
        // sequence all reject.
        void validate_utf8(const std::span<const byte> bytes)
        {
            std::size_t index = 0;
            while (index < bytes.size()) {
                const std::uint8_t lead = std::to_integer<std::uint8_t>(bytes[index]);
                if (lead < 0x80U) {
                    index += 1;
                    continue;
                }
                std::uint32_t continuation_count = 0;
                std::uint32_t code_point = 0;
                std::uint32_t minimum = 0;
                if ((lead & 0xE0U) == 0xC0U) {
                    continuation_count = 1;
                    code_point = lead & 0x1FU;
                    minimum = 0x80U;
                } else if ((lead & 0xF0U) == 0xE0U) {
                    continuation_count = 2;
                    code_point = lead & 0x0FU;
                    minimum = 0x800U;
                } else if ((lead & 0xF8U) == 0xF0U) {
                    continuation_count = 3;
                    code_point = lead & 0x07U;
                    minimum = 0x10000U;
                } else {
                    fail("resource string has an invalid UTF-8 lead byte");
                }
                if (bytes.size() - index <= continuation_count) {
                    fail("resource string has a truncated UTF-8 sequence");
                }
                for (std::uint32_t offset = 1; offset <= continuation_count; ++offset) {
                    const std::uint8_t continuation = std::to_integer<std::uint8_t>(bytes[index + offset]);
                    if ((continuation & 0xC0U) != 0x80U) {
                        fail("resource string has an invalid UTF-8 continuation byte");
                    }
                    code_point = (code_point << 6U) | (continuation & 0x3FU);
                }
                if (code_point < minimum || code_point > 0x10FFFFU ||
                    (code_point >= 0xD800U && code_point <= 0xDFFFU)) {
                    fail("resource string has an invalid UTF-8 code point");
                }
                index += continuation_count + 1;
            }
        }

        std::string parser::read_string_body(const std::uint32_t length)
        {
            if (length == 0) {
                return {};
            }
            if (length > limits_->max_string_bytes) {
                fail("resource string exceeds the string budget");
            }
            const std::span<const byte> span = take(length);
            charge(length);
            if (std::to_integer<std::uint8_t>(span.back()) != 0) {
                fail("resource string is not NUL-terminated");
            }
            const std::span<const byte> content = span.first(span.size() - 1);
            validate_utf8(content);
            std::string result(reinterpret_cast<const char *>(content.data()), content.size());
            if (result.find('\0') != std::string::npos) {
                fail("resource string contains an embedded NUL");
            }
            return result;
        }

        std::string parser::read_unicode_string()
        {
            return read_string_body(u32());
        }

        std::string parser::read_name()
        {
            const std::uint32_t field = u32();
            if (inline_names_ && (field & 0x80000000U) != 0) {
                return read_string_body(field & 0x7FFFFFFFU);
            }
            const std::string &table = string_at(field);
            // Charge the owned copy of the table-backed name before it is built, so repeating
            // one large table entry cannot bypass the aggregate allocation budget.
            charge(table.size() + 1);
            return table;
        }

        resource_value decode_nil(parser &, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            return {};
        }

        resource_value decode_bool(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::boolean;
            value.boolean = reader.u32() != 0;
            return value;
        }

        resource_value decode_int32(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::integer;
            value.integer = static_cast<std::int32_t>(reader.u32());
            return value;
        }

        resource_value decode_int64(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::integer;
            value.integer = static_cast<std::int64_t>(reader.u64());
            return value;
        }

        resource_value decode_real(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::real;
            value.real = reader.read_real();
            return value;
        }

        resource_value decode_double(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::real;
            value.real = reader.read_double();
            return value;
        }

        resource_value decode_string(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::string;
            value.string = reader.read_unicode_string();
            return value;
        }

        resource_value decode_bytes(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(1);
            const std::uint64_t copied =
                reader.metadata_only() ? (std::min)(std::uint64_t{64}, std::uint64_t{count}) : count;
            reader.charge(copied);
            const std::span<const byte> span = reader.take(copied);
            resource_value value;
            value.kind = value_kind::bytes;
            value.byte_offset = reader.position() - copied;
            value.byte_length = count;
            value.bytes.assign(span.begin(), span.end());
            reader.skip(count - copied);
            reader.align4(count);
            return value;
        }

        resource_value decode_int_array32(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(4);
            reader.charge(static_cast<std::uint64_t>(count) * 8);
            resource_value value;
            value.kind = value_kind::integer_array;
            value.integer_array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                value.integer_array.push_back(static_cast<std::int32_t>(reader.u32()));
            }
            return value;
        }

        resource_value decode_int_array64(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(8);
            reader.charge(static_cast<std::uint64_t>(count) * 8);
            resource_value value;
            value.kind = value_kind::integer_array;
            value.integer_array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                value.integer_array.push_back(static_cast<std::int64_t>(reader.u64()));
            }
            return value;
        }

        resource_value decode_real_array_width(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(reader.real_width());
            reader.charge(static_cast<std::uint64_t>(count) * 8);
            resource_value value;
            value.kind = value_kind::real_array;
            value.real_array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                value.real_array.push_back(reader.read_real());
            }
            return value;
        }

        resource_value decode_real_array32(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(4);
            reader.charge(static_cast<std::uint64_t>(count) * 8);
            resource_value value;
            value.kind = value_kind::real_array;
            value.real_array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                value.real_array.push_back(reader.read_float32());
            }
            return value;
        }

        resource_value decode_real_array64(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(8);
            reader.charge(static_cast<std::uint64_t>(count) * 8);
            resource_value value;
            value.kind = value_kind::real_array;
            value.real_array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                value.real_array.push_back(reader.read_double());
            }
            return value;
        }

        resource_value decode_string_array(parser &reader, std::uint32_t depth, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_count(4);
            reader.preflight_children(count, depth + 1);
            reader.charge(static_cast<std::uint64_t>(count) * sizeof(resource_value));
            resource_value value;
            value.kind = value_kind::array;
            value.array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                reader.enter_node();
                resource_value element;
                element.kind = value_kind::string;
                element.string = reader.read_unicode_string();
                value.array.push_back(std::move(element));
            }
            return value;
        }

        resource_value decode_array(parser &reader, std::uint32_t depth, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_shared_count(4);
            reader.preflight_children(count, depth + 1);
            reader.charge(static_cast<std::uint64_t>(count) * sizeof(resource_value));
            resource_value value;
            value.kind = value_kind::array;
            value.array.reserve(count);
            for (std::uint32_t index = 0; index < count; ++index) {
                value.array.push_back(reader.read_value(depth + 1));
            }
            return value;
        }

        resource_value decode_object(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t form = reader.u32();
            resource_value value;
            value.kind = value_kind::reference;
            if (form == object_empty) {
                value.reference.empty = true;
                return value;
            }
            if (form == object_internal_resource) {
                const std::uint32_t field = reader.u32();
                value.reference.index = reader.resolve_internal_reference(field);
                if (reader.legacy_id_mode()) {
                    value.reference.has_legacy_id = true;
                    value.reference.legacy_id = field;
                }
                return value;
            }
            if (form == object_external_resource) {
                value.reference.external = true;
                value.reference.inline_definition = true;
                value.reference.type = reader.read_unicode_string();
                value.reference.path = reader.read_unicode_string();
                return value;
            }
            if (form == object_external_resource_index) {
                const std::uint32_t index = reader.u32();
                reader.require_external_index(index);
                value.reference.external = true;
                value.reference.index = index;
                return value;
            }
            fail("resource object reference has an unknown form");
        }

        resource_value decode_image(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t encoding = reader.u32();
            resource_value value;
            value.kind = value_kind::embedded_image;
            value.image.encoding = encoding;
            if (encoding == 0) {
                value.kind = value_kind::nil;
                return value;
            }
            if (encoding == 1) {
                value.image.width = reader.u32();
                value.image.height = reader.u32();
                value.image.mipmaps = reader.u32();
                const std::uint32_t raw_format = reader.u32();
                if (reader.versioned_image_format()) {
                    if ((raw_format >> 24U) != 0) {
                        fail("embedded image format version is unsupported");
                    }
                    value.image.format = raw_format & 0x00FFFFFFU;
                } else {
                    value.image.format = raw_format;
                }
            }
            const std::uint32_t count = reader.read_count(1);
            const std::uint64_t copied =
                reader.metadata_only() ? (std::min)(std::uint64_t{64}, std::uint64_t{count}) : count;
            reader.charge(copied);
            const std::span<const byte> span = reader.take(copied);
            value.image.byte_offset = reader.position() - copied;
            value.image.byte_length = count;
            value.image.data.assign(span.begin(), span.end());
            reader.skip(count - copied);
            reader.align4(count);
            return value;
        }

        // A NodePath carries two 16-bit counts followed by that many string references; the
        // deprecated pre-format-3 layout adds a trailing property reference to the subname
        // count. It is consumed exactly and represented as unsupported.
        resource_value decode_node_path(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t name_count = reader.u16();
            const std::uint32_t subname_field = reader.u16();
            std::uint32_t subname_count = subname_field & 0x7FFFU;
            if (reader.format_version() < format_version_no_nodepath_property) {
                subname_count += 1;
            }
            for (std::uint32_t index = 0; index < name_count; ++index) {
                (void)reader.read_name();
            }
            for (std::uint32_t index = 0; index < subname_count; ++index) {
                (void)reader.read_name();
            }
            resource_value value;
            value.kind = value_kind::unsupported;
            return value;
        }

        // A modern StringName carries one inline unicode string.
        resource_value decode_string_name(parser &reader, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            (void)reader.read_unicode_string();
            resource_value value;
            value.kind = value_kind::unsupported;
            return value;
        }

        // The deprecated Godot 2 InputEvent carries no payload.
        resource_value decode_input_event(parser &, std::uint32_t, std::uint32_t, std::uint32_t)
        {
            resource_value value;
            value.kind = value_kind::unsupported;
            return value;
        }

        std::uint64_t skip_element_bytes(const parser &reader, const std::uint32_t unit)
        {
            return unit == skip_unit_real ? reader.real_width() : 4;
        }

        resource_value decode_skip_fixed(parser &reader, std::uint32_t, const std::uint32_t unit,
                                         const std::uint32_t components)
        {
            const std::uint64_t element = skip_element_bytes(reader, unit);
            reader.skip(element * components);
            resource_value value;
            value.kind = value_kind::unsupported;
            return value;
        }

        resource_value decode_skip_counted(parser &reader, std::uint32_t, const std::uint32_t unit,
                                           const std::uint32_t components)
        {
            const std::uint64_t element = skip_element_bytes(reader, unit) * components;
            const std::uint32_t count = reader.read_count(element);
            reader.skip(element * count);
            resource_value value;
            value.kind = value_kind::unsupported;
            return value;
        }

        // A dictionary is a shared-counted sequence of key/value Variant pairs. Only a
        // dictionary whose keys are all strings is represented, as an ordered vector of named
        // entries; that is the shape of an Image or Sample ``data`` payload. A structurally
        // valid dictionary with any non-string key is still fully consumed but stays
        // unmodelled, and a duplicate represented key rejects because it would make a media
        // property ambiguous.
        resource_value decode_dictionary(parser &reader, std::uint32_t depth, std::uint32_t, std::uint32_t)
        {
            const std::uint32_t count = reader.read_shared_count(8);
            // Each dictionary pair consumes two child nodes (a key and a value), so the node
            // budget must accommodate ``2 * count`` before the entry vector is charged or
            // reserved.
            reader.preflight_children(static_cast<std::uint64_t>(count) * 2, depth + 1);
            // The retained entry vector keeps a ``resource_property`` per pair, and the pair
            // itself is materialised as two ``resource_value`` temporaries before it is moved
            // in; both the retained slots and the transient pair are charged, so the previous
            // transient accounting is not dropped.
            reader.charge(static_cast<std::uint64_t>(count) * sizeof(resource_property));
            reader.charge(static_cast<std::uint64_t>(count) * sizeof(resource_value));
            resource_value value;
            std::vector<resource_property> entries;
            entries.reserve(count);
            bool string_keys = true;
            for (std::uint32_t index = 0; index < count; ++index) {
                resource_value key = reader.read_value(depth + 1);
                resource_value entry = reader.read_value(depth + 1);
                if (!string_keys || key.kind != value_kind::string) {
                    string_keys = false;
                    continue;
                }
                resource_property property;
                property.name = std::move(key.string);
                property.value = std::move(entry);
                entries.push_back(std::move(property));
            }
            if (!string_keys) {
                value.kind = value_kind::unsupported;
                return value;
            }
            // Reject a duplicate represented key without disturbing the wire order, using a
            // transient index ordered by name so detection stays O(n log n).
            reader.charge(static_cast<std::uint64_t>(count) * sizeof(std::uint32_t));
            std::vector<std::uint32_t> order;
            order.reserve(entries.size());
            for (std::uint32_t index = 0; index < entries.size(); ++index) {
                order.push_back(index);
            }
            std::sort(order.begin(), order.end(), [&entries](const std::uint32_t left, const std::uint32_t right) {
                return entries[left].name < entries[right].name;
            });
            for (std::size_t index = 1; index < order.size(); ++index) {
                if (entries[order[index - 1]].name == entries[order[index]].name) {
                    fail("resource dictionary has a duplicate key");
                }
            }
            value.kind = value_kind::dictionary;
            value.dictionary = std::move(entries);
            return value;
        }

        resource_value parser::read_value(const std::uint32_t depth)
        {
            require_depth(depth);
            enter_node();
            const std::uint32_t raw = u32();
            const std::optional<tag_entry> entry = find_tag(raw);
            if (!entry) {
                fail("resource variant tag is unknown");
            }
            return entry->decode(*this, depth, entry->unit, entry->components);
        }

        void parser::read_header()
        {
            if (has_magic_) {
                require(4);
                const auto magic = read_fixed<4>(0);
                const std::uint8_t m0 = std::to_integer<std::uint8_t>(magic[0]);
                const std::uint8_t m1 = std::to_integer<std::uint8_t>(magic[1]);
                const std::uint8_t m2 = std::to_integer<std::uint8_t>(magic[2]);
                const std::uint8_t m3 = std::to_integer<std::uint8_t>(magic[3]);
                position_ += 4;
                if (m0 == 'R' && m1 == 'S' && m2 == 'C' && m3 == 'C') {
                    fail("compressed resource requires the RSCC wrapper");
                }
                if (m0 != 'R' || m1 != 'S' || m2 != 'R' || m3 != 'C') {
                    fail("input is not an RSRC resource");
                }
            }

            big_endian_ = u32() != 0;
            const std::uint32_t real64_field = u32();
            const std::uint32_t major = u32();
            const std::uint32_t minor = u32();
            const std::uint32_t format = u32();

            if (major != engine_major_legacy_v2 && major != engine_major_legacy_v3 && major != engine_major_modern) {
                fail("unsupported engine major version");
            }
            modern_ = major == engine_major_modern;
            v2_ = major == engine_major_legacy_v2;
            // Godot 2 property names are always string-table indices; Godot 3 and 4 may carry
            // an inline (high-bit) name.
            inline_names_ = !v2_;
            format_version_ = format;
            std::uint32_t max_format = max_format_legacy_v2;
            if (major == engine_major_legacy_v3) {
                max_format = max_format_legacy_v3;
            } else if (modern_) {
                max_format = max_format_modern;
            }
            if (format > max_format) {
                fail("unsupported resource format version");
            }

            result_.engine.major = major;
            result_.engine.minor = minor;
            result_.format_version = format;

            result_.main_type = read_unicode_string();
            const std::uint64_t import_offset = u64();
            if (import_offset > data_size_) {
                fail("import metadata offset is outside the input");
            }

            if (modern_) {
                const std::uint32_t flags = u32();
                real_is_double_ = (flags & format_flag_real_is_double) != 0;
                using_uids_ = (flags & format_flag_uids) != 0;
                const bool has_script_class = (flags & format_flag_has_script_class) != 0;
                named_scene_ids_ = (flags & format_flag_named_scene_ids) != 0;
                (void)u64();
                if (has_script_class) {
                    (void)read_unicode_string();
                }
                for (std::uint32_t index = 0; index < reserved_fields_modern; ++index) {
                    (void)u32();
                }
            } else {
                real_is_double_ = real64_field != 0;
                versioned_image_format_ = major == engine_major_legacy_v3;
                for (std::uint32_t index = 0; index < reserved_fields_legacy; ++index) {
                    (void)u32();
                }
            }
        }

        void parser::read_tables()
        {
            const std::uint32_t string_count = read_count(4);
            charge(static_cast<std::uint64_t>(string_count) * sizeof(std::string));
            strings_.reserve(string_count);
            for (std::uint32_t index = 0; index < string_count; ++index) {
                strings_.push_back(read_unicode_string());
            }

            const std::uint64_t external_minimum = modern_ && using_uids_ ? 16 : 8;
            const std::uint32_t external_count = read_count(external_minimum);
            charge(static_cast<std::uint64_t>(external_count) * sizeof(external_resource));
            result_.external_resources.reserve(external_count);
            for (std::uint32_t index = 0; index < external_count; ++index) {
                external_resource resource;
                resource.type = read_unicode_string();
                resource.path = read_unicode_string();
                if (modern_ && using_uids_) {
                    (void)u64();
                }
                result_.external_resources.push_back(std::move(resource));
            }

            const std::uint32_t internal_count = read_count(12);
            charge(static_cast<std::uint64_t>(internal_count) * sizeof(internal_resource));
            result_.internal_resources.reserve(internal_count);
            for (std::uint32_t index = 0; index < internal_count; ++index) {
                internal_resource resource;
                resource.path = read_unicode_string();
                resource.offset = u64();
                result_.internal_resources.push_back(std::move(resource));
            }
        }

        // Builds the subresource-id index for the legacy addressing modes. Each internal
        // resource whose path is "local://<id>" contributes its id; a malformed id, an
        // out-of-range id or a duplicate id rejects. Named (opaque) ids are not numeric and
        // are only used by the positional mode, so they are never indexed here.
        void parser::build_legacy_ids()
        {
            legacy_ids_.clear();
            // Godot's legacy loader treats the final directory row as the main resource and
            // never interprets its path as a subresource id (Godot 3.6 loader, `if (!main)`).
            const std::size_t subresources =
                result_.internal_resources.empty() ? 0 : result_.internal_resources.size() - 1;
            charge(static_cast<std::uint64_t>(subresources) * sizeof(legacy_id_entry));
            legacy_ids_.reserve(subresources);
            for (std::size_t index = 0; index < subresources; ++index) {
                const std::string &path = result_.internal_resources[index].path;
                if (path.rfind("local://", 0) != 0) {
                    continue;
                }
                const std::string_view suffix = std::string_view(path).substr(8);
                const bool negative = !suffix.empty() && suffix.front() == '-';
                const std::string_view digits = suffix.substr(negative ? 1 : 0);
                if (digits.empty()) {
                    fail("internal resource id is malformed");
                }
                std::uint64_t magnitude = 0;
                for (const char digit : digits) {
                    if (digit < '0' || digit > '9') {
                        fail("internal resource id is malformed");
                    }
                    magnitude = magnitude * 10 + static_cast<std::uint64_t>(digit - '0');
                    if (magnitude > (negative ? 0x80000000ULL : 0xFFFFFFFFULL)) {
                        fail("internal resource id is out of range");
                    }
                }
                const auto id = static_cast<std::uint32_t>(magnitude);
                legacy_ids_.push_back({negative ? std::uint32_t{0} - id : id, static_cast<std::uint32_t>(index)});
            }
            std::sort(legacy_ids_.begin(), legacy_ids_.end(),
                      [](const legacy_id_entry &left, const legacy_id_entry &right) { return left.id < right.id; });
            for (std::size_t index = 1; index < legacy_ids_.size(); ++index) {
                if (legacy_ids_[index - 1].id == legacy_ids_[index].id) {
                    fail("duplicate internal resource id");
                }
            }
        }

        void parser::read_records()
        {
            if (legacy_id_mode()) {
                build_legacy_ids();
            }
            charge(static_cast<std::uint64_t>(result_.internal_resources.size()) * sizeof(resource_record));
            result_.records.reserve(result_.internal_resources.size());
            for (const internal_resource &resource : result_.internal_resources) {
                seek(resource.offset);
                resource_record record;
                // Charge the owned copy of the directory path before building it.
                charge(resource.path.size() + 1);
                record.path = resource.path;
                record.type = read_unicode_string();
                const std::uint32_t property_count = read_count(8);
                preflight_children(property_count, 1);
                charge(static_cast<std::uint64_t>(property_count) * sizeof(resource_property));
                record.properties.reserve(property_count);
                for (std::uint32_t index = 0; index < property_count; ++index) {
                    resource_property property;
                    property.name = read_name();
                    if (property.name.empty()) {
                        fail("resource property name is empty");
                    }
                    property.value = read_value(1);
                    record.properties.push_back(std::move(property));
                }
                result_.records.push_back(std::move(record));
            }
        }

        binary_resource parser::parse()
        {
            if (data_size_ > limits_->max_input_bytes) {
                fail("resource exceeds the input budget");
            }
            read_header();
            read_tables();
            read_records();
            return std::move(result_);
        }

        std::optional<binary_resource> parser::parse_if(const std::function<bool(std::string_view)> &accept_main_type)
        {
            if (data_size_ > limits_->max_input_bytes) {
                fail("resource exceeds the input budget");
            }
            read_header();
            if (!accept_main_type(result_.main_type)) {
                return std::nullopt;
            }
            read_tables();
            read_records();
            return std::move(result_);
        }
    } // namespace

    binary_resource read_binary_resource(const std::span<const byte> data, const bool has_magic,
                                         const resource_limits &limits)
    {
        parser reader(data, has_magic, limits);
        return reader.parse();
    }

    binary_resource read_binary_resource_metadata(const std::uint64_t logical_size,
                                                  const std::function<void(std::uint64_t, std::span<byte>)> &read_at,
                                                  const bool has_magic, const resource_limits &limits)
    {
        parser reader(logical_size, read_at, has_magic, limits);
        return reader.parse();
    }

    std::optional<binary_resource> read_binary_resource_metadata_if(
        const std::uint64_t logical_size, const std::function<void(std::uint64_t, std::span<byte>)> &read_at,
        const std::function<bool(std::string_view)> &accept_main_type, const bool has_magic,
        const resource_limits &limits)
    {
        parser reader(logical_size, read_at, has_magic, limits);
        return reader.parse_if(accept_main_type);
    }
} // namespace unpackrat::godot
