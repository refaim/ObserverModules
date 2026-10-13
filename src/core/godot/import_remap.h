#pragma once

#include <cstddef>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace unpackrat::godot
{
    // One virtual resource path bound to one compiled resource inside the archive.
    // ``variant`` is empty for the base ``path`` key and holds the feature suffix
    // (``s3tc``, ``etc2``, ...) for ``path.<feature>`` keys.
    struct import_mapping
    {
        std::string virtual_path;
        std::string compiled_path;
        std::string variant;
    };

    struct metadata_limits
    {
        std::size_t max_input_bytes = std::size_t{4} * 1024 * 1024;
        std::size_t max_mappings = 128;
        std::size_t max_nesting = 16;
    };

    class metadata_error final : public std::runtime_error
    {
      public:
        metadata_error() : std::runtime_error("godot import/remap metadata is malformed")
        {
        }
    };

    // Parse a bounded Godot ``.import``/``.remap`` ConfigFile fragment. The
    // metadata never touches the filesystem or evaluates any script/resource.
    [[nodiscard]] std::vector<import_mapping> read_import_remap(std::span<const std::byte> bytes,
                                                                std::string_view metadata_path,
                                                                const metadata_limits &limits = {});
} // namespace unpackrat::godot
