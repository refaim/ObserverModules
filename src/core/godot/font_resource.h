#ifndef UNPACKRAT_CORE_GODOT_FONT_RESOURCE_H
#define UNPACKRAT_CORE_GODOT_FONT_RESOURCE_H

#include "binary_resource.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>

namespace unpackrat::godot
{
    // Conservative class-only admission for a root FontFile binary resource.
    [[nodiscard]] bool supports_font_resource_class(std::string_view class_name) noexcept;

    class font_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    struct font_payload_info final
    {
        std::string extension;
        std::uint64_t byte_offset = 0;
        std::uint64_t byte_length = 0;
    };

    // Describes the validated embedded font range in the logical resource source.
    [[nodiscard]] std::optional<font_payload_info> inspect_font_resource(const binary_resource &resource);

    // Returns the embedded font's format from its capture bytes, without copying or rendering it.
    [[nodiscard]] std::optional<std::string_view> font_resource_extension(const binary_resource &resource);

    // Forwards the original embedded font bytes only during extraction.
    void write_font_resource(const binary_resource &resource,
                             const std::function<void(std::span<const std::byte>)> &sink);
} // namespace unpackrat::godot
#endif
