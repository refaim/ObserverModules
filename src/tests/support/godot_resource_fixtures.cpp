#include "godot_resource_fixtures.h"

#include <algorithm>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <string>
#include <string_view>
#include <utility>

namespace test::support
{
    namespace
    {
        constexpr std::uint32_t modern_layout_major = 4;

        void append_text(godot_resource_bytes &bytes, std::string_view text)
        {
            std::transform(text.begin(), text.end(), std::back_inserter(bytes), [](const char character) {
                return static_cast<std::uint8_t>(static_cast<unsigned char>(character));
            });
        }
    } // namespace

    wire_writer::wire_writer(const bool big_endian, const bool real64) : big_endian_(big_endian), real64_(real64)
    {
    }

    wire_writer &wire_writer::u8(const std::uint8_t value)
    {
        bytes_.push_back(value);
        return *this;
    }

    wire_writer &wire_writer::u16(const std::uint16_t value)
    {
        if (big_endian_) {
            bytes_.push_back(static_cast<std::uint8_t>(value >> 8U));
            bytes_.push_back(static_cast<std::uint8_t>(value));
        } else {
            bytes_.push_back(static_cast<std::uint8_t>(value));
            bytes_.push_back(static_cast<std::uint8_t>(value >> 8U));
        }
        return *this;
    }

    wire_writer &wire_writer::u32(const std::uint32_t value)
    {
        if (big_endian_) {
            bytes_.push_back(static_cast<std::uint8_t>(value >> 24U));
            bytes_.push_back(static_cast<std::uint8_t>(value >> 16U));
            bytes_.push_back(static_cast<std::uint8_t>(value >> 8U));
            bytes_.push_back(static_cast<std::uint8_t>(value));
        } else {
            bytes_.push_back(static_cast<std::uint8_t>(value));
            bytes_.push_back(static_cast<std::uint8_t>(value >> 8U));
            bytes_.push_back(static_cast<std::uint8_t>(value >> 16U));
            bytes_.push_back(static_cast<std::uint8_t>(value >> 24U));
        }
        return *this;
    }

    wire_writer &wire_writer::u64(const std::uint64_t value)
    {
        if (big_endian_) {
            u32(static_cast<std::uint32_t>(value >> 32U));
            u32(static_cast<std::uint32_t>(value));
        } else {
            u32(static_cast<std::uint32_t>(value));
            u32(static_cast<std::uint32_t>(value >> 32U));
        }
        return *this;
    }

    wire_writer &wire_writer::real(const double value)
    {
        return real64_ ? double64(value) : float32(static_cast<float>(value));
    }

    wire_writer &wire_writer::float32(const float value)
    {
        return u32(std::bit_cast<std::uint32_t>(value));
    }

    wire_writer &wire_writer::double64(const double value)
    {
        return u64(std::bit_cast<std::uint64_t>(value));
    }

    wire_writer &wire_writer::raw(const std::span<const std::uint8_t> bytes)
    {
        bytes_.insert(bytes_.end(), bytes.begin(), bytes.end());
        return *this;
    }

    wire_writer &wire_writer::pad4(const std::uint64_t length)
    {
        const std::uint64_t padding = (4 - (length % 4)) % 4;
        for (std::uint64_t index = 0; index < padding; ++index) {
            bytes_.push_back(0);
        }
        return *this;
    }

    wire_writer &wire_writer::unicode(const std::string_view text)
    {
        u32(static_cast<std::uint32_t>(text.size() + 1));
        append_text(bytes_, text);
        bytes_.push_back(0);
        return *this;
    }

    wire_writer &wire_writer::inline_unicode(const std::string_view text)
    {
        u32(0x80000000U | static_cast<std::uint32_t>(text.size() + 1));
        append_text(bytes_, text);
        bytes_.push_back(0);
        return *this;
    }

    void wire_writer::patch_u32(const std::size_t offset, const std::uint32_t value)
    {
        const godot_resource_bytes patch =
            big_endian_
                ? godot_resource_bytes{static_cast<std::uint8_t>(value >> 24U), static_cast<std::uint8_t>(value >> 16U),
                                       static_cast<std::uint8_t>(value >> 8U), static_cast<std::uint8_t>(value)}
                : godot_resource_bytes{static_cast<std::uint8_t>(value), static_cast<std::uint8_t>(value >> 8U),
                                       static_cast<std::uint8_t>(value >> 16U),
                                       static_cast<std::uint8_t>(value >> 24U)};
        std::copy(patch.begin(), patch.end(), bytes_.begin() + static_cast<std::ptrdiff_t>(offset));
    }

    void wire_writer::patch_u64(const std::size_t offset, const std::uint64_t value)
    {
        if (big_endian_) {
            patch_u32(offset, static_cast<std::uint32_t>(value >> 32U));
            patch_u32(offset + 4, static_cast<std::uint32_t>(value));
        } else {
            patch_u32(offset, static_cast<std::uint32_t>(value));
            patch_u32(offset + 4, static_cast<std::uint32_t>(value >> 32U));
        }
    }

    std::size_t wire_writer::size() const noexcept
    {
        return bytes_.size();
    }

    const godot_resource_bytes &wire_writer::bytes() const noexcept
    {
        return bytes_;
    }

    godot_resource_bytes wire_writer::take()
    {
        return std::move(bytes_);
    }

    variant_encoder::variant_encoder(wire_writer &out, const bool modern, const bool real64)
        : out_(out), modern_(modern), real64_(real64)
    {
    }

    namespace
    {
        void write_real(wire_writer &out, const bool real64, const double value)
        {
            if (real64) {
                out.double64(value);
            } else {
                out.float32(static_cast<float>(value));
            }
        }
    } // namespace

    variant_encoder &variant_encoder::nil()
    {
        return raw_tag(godot_tag_nil);
    }

    variant_encoder &variant_encoder::boolean(const bool value)
    {
        out_.u32(godot_tag_bool).u32(value ? 1U : 0U);
        return *this;
    }

    variant_encoder &variant_encoder::int32(const std::int32_t value)
    {
        out_.u32(godot_tag_int).u32(static_cast<std::uint32_t>(value));
        return *this;
    }

    variant_encoder &variant_encoder::int64(const std::int64_t value)
    {
        out_.u32(godot_tag_int64).u64(static_cast<std::uint64_t>(value));
        return *this;
    }

    variant_encoder &variant_encoder::real_value(const double value)
    {
        out_.u32(godot_tag_real);
        write_real(out_, real64_, value);
        return *this;
    }

    variant_encoder &variant_encoder::double_value(const double value)
    {
        out_.u32(godot_tag_double).double64(value);
        return *this;
    }

    variant_encoder &variant_encoder::string(const std::string_view value)
    {
        out_.u32(godot_tag_string).unicode(value);
        return *this;
    }

    variant_encoder &variant_encoder::bytes(const std::span<const std::uint8_t> value)
    {
        out_.u32(godot_tag_raw_array).u32(static_cast<std::uint32_t>(value.size()));
        out_.raw(value).pad4(value.size());
        return *this;
    }

    variant_encoder &variant_encoder::int32_array(const std::span<const std::int32_t> values)
    {
        out_.u32(godot_tag_int_array).u32(static_cast<std::uint32_t>(values.size()));
        for (const std::int32_t value : values) {
            out_.u32(static_cast<std::uint32_t>(value));
        }
        return *this;
    }

    variant_encoder &variant_encoder::int64_array(const std::span<const std::int64_t> values)
    {
        out_.u32(godot_tag_packed_int64_array).u32(static_cast<std::uint32_t>(values.size()));
        for (const std::int64_t value : values) {
            out_.u64(static_cast<std::uint64_t>(value));
        }
        return *this;
    }

    variant_encoder &variant_encoder::real_array(const std::span<const double> values)
    {
        out_.u32(godot_tag_real_array).u32(static_cast<std::uint32_t>(values.size()));
        for (const double value : values) {
            if (modern_) {
                out_.float32(static_cast<float>(value));
            } else {
                write_real(out_, real64_, value);
            }
        }
        return *this;
    }

    variant_encoder &variant_encoder::float64_array(const std::span<const double> values)
    {
        out_.u32(godot_tag_packed_float64_array).u32(static_cast<std::uint32_t>(values.size()));
        for (const double value : values) {
            out_.double64(value);
        }
        return *this;
    }

    variant_encoder &variant_encoder::string_array(const std::span<const std::string> values)
    {
        out_.u32(godot_tag_string_array).u32(static_cast<std::uint32_t>(values.size()));
        for (const std::string &value : values) {
            out_.unicode(value);
        }
        return *this;
    }

    variant_encoder &variant_encoder::array(const std::span<const godot_resource_bytes> elements)
    {
        out_.u32(godot_tag_array).u32(static_cast<std::uint32_t>(elements.size()));
        for (const godot_resource_bytes &element : elements) {
            out_.raw(element);
        }
        return *this;
    }

    variant_encoder &variant_encoder::object_empty()
    {
        out_.u32(godot_tag_object).u32(godot_object_empty);
        return *this;
    }

    variant_encoder &variant_encoder::object_internal(const std::uint32_t index)
    {
        out_.u32(godot_tag_object).u32(godot_object_internal_resource).u32(index);
        return *this;
    }

    variant_encoder &variant_encoder::object_external(const std::string_view type, const std::string_view path)
    {
        out_.u32(godot_tag_object).u32(godot_object_external_resource).unicode(type).unicode(path);
        return *this;
    }

    variant_encoder &variant_encoder::object_external_index(const std::uint32_t index)
    {
        out_.u32(godot_tag_object).u32(godot_object_external_resource_index).u32(index);
        return *this;
    }

    variant_encoder &variant_encoder::image_raw(const std::uint32_t width, const std::uint32_t height,
                                                const std::uint32_t mipmaps, const std::uint32_t format,
                                                const std::span<const std::uint8_t> data)
    {
        out_.u32(godot_tag_image).u32(1).u32(width).u32(height).u32(mipmaps).u32(format);
        out_.u32(static_cast<std::uint32_t>(data.size()));
        out_.raw(data).pad4(data.size());
        return *this;
    }

    variant_encoder &variant_encoder::image_encoded(const std::uint32_t encoding,
                                                    const std::span<const std::uint8_t> data)
    {
        out_.u32(godot_tag_image).u32(encoding).u32(static_cast<std::uint32_t>(data.size()));
        out_.raw(data).pad4(data.size());
        return *this;
    }

    variant_encoder &variant_encoder::vector2(const double x, const double y)
    {
        out_.u32(godot_tag_vector2);
        write_real(out_, real64_, x);
        write_real(out_, real64_, y);
        return *this;
    }

    variant_encoder &variant_encoder::rid(const std::uint32_t id)
    {
        out_.u32(godot_tag_rid).u32(id);
        return *this;
    }

    variant_encoder &variant_encoder::input_event()
    {
        out_.u32(godot_tag_input_event);
        return *this;
    }

    variant_encoder &variant_encoder::node_path(const std::span<const std::uint32_t> names,
                                                const std::span<const std::uint32_t> subnames, const bool absolute,
                                                const bool extra_property)
    {
        out_.u32(godot_tag_node_path);
        out_.u16(static_cast<std::uint16_t>(names.size()));
        std::uint16_t field = static_cast<std::uint16_t>(subnames.size());
        if (absolute) {
            field = static_cast<std::uint16_t>(field | 0x8000U);
        }
        out_.u16(field);
        for (const std::uint32_t id : names) {
            out_.u32(id);
        }
        for (const std::uint32_t id : subnames) {
            out_.u32(id);
        }
        if (extra_property) {
            out_.u32(0);
        }
        return *this;
    }

    variant_encoder &variant_encoder::string_name(const std::string_view value)
    {
        out_.u32(godot_tag_string_name).unicode(value);
        return *this;
    }

    variant_encoder &variant_encoder::packed_vector3_array(const std::uint32_t count)
    {
        out_.u32(godot_tag_vector3_array).u32(count);
        for (std::uint32_t index = 0; index < count; ++index) {
            out_.real(0.5).real(1.5).real(2.5);
        }
        return *this;
    }

    variant_encoder &variant_encoder::packed_vector2_array(const std::uint32_t count)
    {
        out_.u32(godot_tag_vector2_array).u32(count);
        for (std::uint32_t index = 0; index < count; ++index) {
            out_.real(0.25).real(0.75);
        }
        return *this;
    }

    variant_encoder &variant_encoder::packed_color_array(const std::uint32_t count)
    {
        out_.u32(godot_tag_color_array).u32(count);
        for (std::uint32_t index = 0; index < count; ++index) {
            out_.float32(0.0F).float32(0.5F).float32(1.0F).float32(1.0F);
        }
        return *this;
    }

    variant_encoder &variant_encoder::dictionary(const std::span<const godot_resource_bytes> keys_and_values)
    {
        out_.u32(godot_tag_dictionary).u32(static_cast<std::uint32_t>(keys_and_values.size() / 2));
        for (const godot_resource_bytes &entry : keys_and_values) {
            out_.raw(entry);
        }
        return *this;
    }

    variant_encoder &variant_encoder::string_dictionary(
        const std::span<const std::pair<std::string, godot_resource_bytes>> entries, const bool shared)
    {
        std::uint32_t field = static_cast<std::uint32_t>(entries.size());
        if (shared) {
            field |= 0x80000000U;
        }
        out_.u32(godot_tag_dictionary).u32(field);
        for (const auto &[name, value] : entries) {
            out_.u32(godot_tag_string).unicode(name);
            out_.raw(value);
        }
        return *this;
    }

    variant_encoder &variant_encoder::raw_tag(const std::uint32_t tag)
    {
        out_.u32(tag);
        return *this;
    }

    godot_resource_bytes build_resource(const resource_document_spec &spec)
    {
        const bool modern = spec.engine_major >= modern_layout_major;
        wire_writer out(spec.big_endian, spec.use_real64);

        if (spec.include_magic) {
            out.raw(godot_resource_bytes{'R', 'S', 'R', 'C'});
        }
        out.u32(spec.big_endian ? 1U : 0U);
        out.u32(spec.use_real64 ? 1U : 0U);
        out.u32(spec.engine_major);
        out.u32(spec.engine_minor);
        out.u32(spec.format_version);
        out.unicode(spec.main_type);
        out.u64(spec.import_offset.value_or(0));
        if (modern) {
            std::uint32_t flags = spec.format_flags;
            if (spec.real64_flag) {
                flags |= 4U;
            }
            if (spec.modern_external_uids) {
                flags |= 2U;
            }
            if (spec.named_scene_ids) {
                flags |= 1U;
            }
            if (spec.has_script_class) {
                flags |= 8U;
            }
            out.u32(flags);
            out.u64(0);
            if (spec.has_script_class) {
                out.unicode(spec.script_class);
            }
            for (std::uint32_t index = 0; index < 11; ++index) {
                out.u32(0);
            }
        } else {
            for (std::uint32_t index = 0; index < 14; ++index) {
                out.u32(0);
            }
        }

        out.u32(spec.string_table_count.value_or(static_cast<std::uint32_t>(spec.string_table.size())));
        for (const std::string &entry : spec.string_table) {
            out.unicode(entry);
        }

        out.u32(spec.external_count.value_or(static_cast<std::uint32_t>(spec.external_resources.size())));
        for (const auto &[type, path] : spec.external_resources) {
            out.unicode(type);
            out.unicode(path);
            if (modern && spec.modern_external_uids) {
                out.u64(0);
            }
        }

        out.u32(spec.internal_count.value_or(static_cast<std::uint32_t>(spec.records.size())));
        std::vector<std::size_t> offset_positions;
        for (const resource_record_spec &record : spec.records) {
            out.unicode(record.path);
            offset_positions.push_back(out.size());
            out.u64(0);
        }

        std::vector<std::uint64_t> record_offsets;
        for (const resource_record_spec &record : spec.records) {
            record_offsets.push_back(static_cast<std::uint64_t>(out.size()));
            out.unicode(record.type);
            out.u32(record.property_count.value_or(static_cast<std::uint32_t>(record.properties.size())));
            for (const resource_property_spec &property : record.properties) {
                if (property.name_field.has_value()) {
                    out.u32(*property.name_field);
                } else {
                    const auto found = std::find(spec.string_table.begin(), spec.string_table.end(), property.name);
                    if (found != spec.string_table.end()) {
                        out.u32(static_cast<std::uint32_t>(std::distance(spec.string_table.begin(), found)));
                    } else if (spec.engine_major >= 3) {
                        out.inline_unicode(property.name);
                    } else {
                        out.u32(0);
                    }
                }
                out.raw(property.value);
            }
        }

        for (std::size_t index = 0; index < spec.records.size(); ++index) {
            out.patch_u64(offset_positions[index], spec.internal_offset.value_or(record_offsets[index]));
        }
        return out.take();
    }
} // namespace test::support
