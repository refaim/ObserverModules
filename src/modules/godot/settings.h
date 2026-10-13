#pragma once

#include <cstddef>

namespace unpackrat::godot
{
    inline constexpr std::size_t module_settings_max_chars = std::size_t{64} * 1024;

    struct module_settings final
    {
        bool decode_resources = false;
    };

    // Parses the host's double-NUL-terminated key=value section without reading files.
    // Null, missing, malformed or over-bound input disables decoding. The first
    // case-insensitive DecodeResources key enables it only for the trimmed value 1.
    [[nodiscard]] module_settings parse_module_settings(const wchar_t *multi_sz) noexcept;
} // namespace unpackrat::godot
