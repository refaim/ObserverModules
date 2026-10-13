#include "font_resource.h"

#include <cstddef>
#include <optional>
#include <span>
#include <string_view>

namespace unpackrat::godot
{
    namespace
    {
        struct font_view final
        {
            std::string_view extension;
            const resource_value *data = nullptr;
        };

        std::optional<font_view> resolve_font(const binary_resource &resource)
        {
            if (resource.records.empty()) {
                return std::nullopt;
            }
            const resource_record &root = resource.records.back();
            if (!supports_font_resource_class(root.type)) {
                return std::nullopt;
            }
            if (resource.main_type != root.type) {
                throw font_error("godot font: root class differs from main type");
            }

            const resource_value *data = nullptr;
            for (const resource_property &property : root.properties) {
                if (property.name == "data" || property.name == "data/0") {
                    if (data != nullptr) {
                        throw font_error("godot font: ambiguous font data property");
                    }
                    data = &property.value;
                }
            }
            if (data == nullptr) {
                return std::nullopt;
            }
            if (data->kind != value_kind::bytes) {
                throw font_error("godot font: data is not a byte array");
            }
            if (data->byte_length != 0 && data->byte_length < data->bytes.size()) {
                throw font_error("godot font: declared data length is shorter than the captured bytes");
            }
            const auto bytes = std::span<const std::byte>(data->bytes.data(), data->bytes.size());
            if (bytes.size() < 4) {
                return std::nullopt;
            }
            const auto prefix = std::string_view(reinterpret_cast<const char *>(bytes.data()), 4);
            if (prefix == std::string_view("\x00\x01\x00\x00", 4) || prefix == "true") {
                return font_view{"ttf", data};
            }
            if (prefix == "OTTO") {
                return font_view{"otf", data};
            }
            if (prefix == "ttcf") {
                return font_view{"ttc", data};
            }
            if (prefix == "wOFF") {
                return font_view{"woff", data};
            }
            if (prefix == "wOF2") {
                return font_view{"woff2", data};
            }
            return std::nullopt;
        }
    } // namespace

    std::optional<font_payload_info> inspect_font_resource(const binary_resource &resource)
    {
        const auto font = resolve_font(resource);
        if (!font.has_value()) {
            return std::nullopt;
        }
        const resource_value &data = *font->data;
        return font_payload_info{std::string(font->extension), data.byte_offset,
                                 data.byte_length == 0 ? data.bytes.size() : data.byte_length};
    }

    std::optional<std::string_view> font_resource_extension(const binary_resource &resource)
    {
        const auto font = resolve_font(resource);
        return font.has_value() ? std::optional<std::string_view>(font->extension) : std::nullopt;
    }

    void write_font_resource(const binary_resource &resource,
                             const std::function<void(std::span<const std::byte>)> &sink)
    {
        const auto font = resolve_font(resource);
        if (!font.has_value()) {
            throw font_error("godot font: no supported embedded font");
        }
        sink(std::span<const std::byte>(font->data->bytes.data(), font->data->bytes.size()));
    }
    bool supports_font_resource_class(const std::string_view class_name) noexcept
    {
        return class_name == "FontFile";
    }
} // namespace unpackrat::godot
