#ifndef UNPACKRAT_MODULES_GODOT_VIRTUAL_TREE_H
#define UNPACKRAT_MODULES_GODOT_VIRTUAL_TREE_H

// Godot PCK virtual presentation tree.
//
// This is the Observer-facing presentation adapter for the Godot module. It turns a
// parsed, content-free PCK index plus the archive's import/remap mappings into a flat,
// deterministic listing: supported resources appear decoded, with a raw fallback for
// everything else. Without conversion hints all files stay raw. Import/remap metadata
// restores resource folders; patch removals are preserved in generated metadata.
//
// The header stays platform-neutral: it exposes only project-owned value types. No Win32
// types, no third-party types and no archive bytes cross this interface. Nothing here
// reads or converts archive contents; the caller binds a source index to the stored bytes.

#include <cstddef>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

#include "../../core/godot/import_remap.h"
#include "../../core/godot/pck.h"

namespace unpackrat::godot
{
    // The caller has structurally recognised that the physical entry at ``source_index``
    // currently holds a directly viewable stream of ``extension`` (one of
    // ``.png``/``.webp``/``.wav``/``.mp3``/``.ogg`` and supported font extensions). The tree never inspects the bytes;
    // it only trusts the caller's already completed recognition.
    struct conversion_hint final
    {
        std::size_t source_index = 0;
        std::string extension;
    };

    // One presented listing row. ``source_index`` binds the row to a physical PCK entry
    // (index 0 is a real file, never a sentinel). It is absent only for a generated
    // path-mapping metadata file, whose prepared bytes live in ``generated_text``.
    struct virtual_entry final
    {
        std::string path;
        std::optional<std::size_t> source_index;
        bool converted = false;
        std::string generated_text;
    };

    struct virtual_tree final
    {
        std::vector<virtual_entry> entries;
    };

    // Presentation budgets. A limit breach fails the whole tree with ``tree_error`` so no
    // physical file or removal record is ever silently dropped.
    struct tree_limits final
    {
        std::size_t max_entries = 1'000'000;
        std::size_t max_metadata_bytes = std::size_t{64} * 1024 * 1024;
        std::size_t max_path_chars = 1023;
    };

    // A presentation fault. Structural faults (an invalid mapping, an impossible path or a breached
    // entry limit) use this base directly, while a metadata-budget breach uses the typed subtype
    // below so a caller can retry with fewer optional rows instead of losing the whole listing.
    class tree_error : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Thrown only for a metadata-budget breach: a retained-metadata charge, a slot reservation or
    // the metadata-size preflight. Nothing else is one, so a caller that catches it and rebuilds the
    // tree never masks a structural presentation fault.
    class tree_budget_error final : public tree_error
    {
      public:
        explicit tree_budget_error(const char *message) : tree_error(message)
        {
        }
    };

    // Builds the deterministic presentation tree for a parsed PCK index. The mappings and
    // hints are borrowed views; the result owns every presented name.
    [[nodiscard]] virtual_tree make_virtual_tree(const pck_index &index, std::span<const import_mapping> mappings,
                                                 std::span<const conversion_hint> hints,
                                                 const tree_limits &limits = {});
} // namespace unpackrat::godot
#endif
