#include "virtual_tree.h"

#include <algorithm>
#include <cstddef>
#include <iterator>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

#include <windows.h>

namespace unpackrat::godot
{
    namespace
    {
        // Budget accounting is deliberately coarse: every retained structure is charged with
        // a small fixed node overhead plus its byte content, and a breach fails the whole
        // tree.
        constexpr std::size_t entry_slot_overhead = 64;
        constexpr std::size_t row_node_overhead = 64;
        constexpr std::size_t name_node_overhead = 48;
        constexpr std::size_t map_node_overhead = 48;
        // Windows limits a single path component to 255 UTF-16 code units; the aggregate
        // ``max_path_chars`` ceiling is applied on top of that per-component rule.
        constexpr std::size_t max_component_chars = 255;
        constexpr std::string_view manifest_name = "name-map.tsv";
        constexpr std::string_view empty_name_placeholder = "%00";
        constexpr char hex_digits[] = "0123456789ABCDEF";
        constexpr std::string_view::size_type npos = std::string_view::npos;

        // Retained-metadata budget. ``charge`` is called before every reserve, emplace,
        // append or widening so a hostile directory cannot drive allocation past the limit.
        class budget final
        {
          public:
            explicit budget(const std::size_t limit) : limit_(limit)
            {
            }

            void charge(const std::size_t bytes)
            {
                if (bytes > limit_ - used_) {
                    throw tree_budget_error("godot virtual tree exceeds the metadata budget");
                }
                used_ += bytes;
            }

            // Charge ``count`` uniform slots of ``slot_bytes`` bytes each. The limit is
            // compared by division so a hostile count cannot overflow the product, and the
            // charge lands before the caller reserves the matching backing store. ``slot_bytes``
            // is always a nonzero ``sizeof`` constant.
            void reserve_slots(const std::size_t count, const std::size_t slot_bytes)
            {
                if (count > (limit_ - used_) / slot_bytes) {
                    throw tree_budget_error("godot virtual tree exceeds the metadata budget");
                }
                used_ += count * slot_bytes;
            }

          private:
            std::size_t limit_;
            std::size_t used_ = 0;
        };

        bool is_valid_utf8(const std::string_view text)
        {
            if (text.empty()) {
                return true;
            }
            return ::MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()),
                                         nullptr, 0) > 0;
        }

        // UTF-16 length without allocating a buffer. Presented names are always valid UTF-8
        // sub-views of an owning string, so the data pointer is valid and the conversion never
        // fails; the return value is the code-unit count.
        std::size_t utf16_length(const std::string_view text)
        {
            return static_cast<std::size_t>(
                ::MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0));
        }

        // Callers pass nonempty names: escape_path supplies a placeholder, placement supplies
        // numeric fallbacks, and is_device_name rejects an empty stem before reaching here.
        std::wstring widen(const std::string_view text)
        {
            const int length =
                ::MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
            std::wstring wide(static_cast<std::size_t>(length), L'\0');
            ::MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), length);
            return wide;
        }

        // Native Windows ordinal comparison. This is intentionally not an ASCII-only
        // lowercasing, so non-ASCII case variants collide exactly as the destination
        // filesystem would.
        bool ordinal_equal(const std::wstring_view left, const std::wstring_view right)
        {
            return ::CompareStringOrdinal(left.data(), static_cast<int>(left.size()), right.data(),
                                          static_cast<int>(right.size()), TRUE) == CSTR_EQUAL;
        }

        // Case-insensitive ordinal ordering for an associative container. Equivalent (case
        // variant) names compare as neither-less, so a ``std::set`` treats them as one key
        // and the lookup stays O(log n) instead of a linear scan per bucket.
        struct ordinal_less final
        {
            using is_transparent = void;

            bool operator()(const std::wstring_view left, const std::wstring_view right) const
            {
                return ::CompareStringOrdinal(left.data(), static_cast<int>(left.size()), right.data(),
                                              static_cast<int>(right.size()), TRUE) == CSTR_LESS_THAN;
            }
        };

        using name_set = std::set<std::wstring, ordinal_less>;

        void insert_name(name_set &set, const std::string_view text, budget &pool)
        {
            std::wstring key = widen(text);
            pool.charge(key.size() * 2 + name_node_overhead);
            set.insert(std::move(key));
        }

        bool is_unsafe_ascii(const unsigned char byte)
        {
            switch (byte) {
            case '<':
            case '>':
            case ':':
            case '"':
            case '|':
            case '?':
            case '*':
            case '/':
            case '\\':
                return true;
            default:
                return false;
            }
        }

        // A single trailing digit that Windows treats as a ``COM``/``LPT`` index: the ASCII
        // digits 1-9 plus the Latin-1 superscript digits 1-3.
        bool is_reserved_device_suffix(const std::wstring_view suffix)
        {
            if (suffix.size() != 1) {
                return false;
            }
            const wchar_t code = suffix[0];
            if (code >= L'1' && code <= L'9') {
                return true;
            }
            return code == L'\u00B9' || code == L'\u00B2' || code == L'\u00B3';
        }

        // Windows reserves ``CON``/``PRN``/``AUX``/``NUL`` and ``COM``/``LPT`` followed by a
        // digit, including the Unicode superscript digits and any extension, independent of
        // case. Such a stem is escaped so the presented name is an ordinary file name.
        bool is_device_name(const std::string_view stem)
        {
            if (stem.empty()) {
                return false;
            }
            const std::wstring wide = widen(stem);
            static constexpr std::wstring_view bases[] = {L"CON", L"PRN", L"AUX", L"NUL"};
            if (std::any_of(std::begin(bases), std::end(bases),
                            [&wide](const std::wstring_view base) { return ordinal_equal(wide, base); })) {
                return true;
            }
            if (wide.size() >= 4) {
                const std::wstring_view prefix(wide.data(), 3);
                if (ordinal_equal(prefix, L"COM") || ordinal_equal(prefix, L"LPT")) {
                    return is_reserved_device_suffix(std::wstring_view(wide.data() + 3, wide.size() - 3));
                }
            }
            return false;
        }

        // Reversibly percent-escapes one path component so it is a safe, ordinary Windows
        // name: unsafe ASCII, the escape character itself, control bytes, trailing dots and
        // spaces, and reserved device names are all encoded. A component that is not valid
        // UTF-8 is conservatively escaped byte by byte, which also makes the result ASCII.
        std::string escape_component(const std::string_view component)
        {
            const bool valid = is_valid_utf8(component);
            const bool dot_pair = component == "." || component == "..";
            std::size_t trailing = 0;
            while (trailing < component.size() && (component[component.size() - 1 - trailing] == '.' ||
                                                   component[component.size() - 1 - trailing] == ' ')) {
                ++trailing;
            }
            const bool device = is_device_name(component.substr(0, component.find('.')));

            std::string out;
            out.reserve(component.size());
            for (std::size_t index = 0; index < component.size(); ++index) {
                const unsigned char byte = static_cast<unsigned char>(component[index]);
                const bool at_trailing = trailing != 0 && index >= component.size() - trailing;
                const bool escape_byte = !valid || dot_pair || byte < 0x20U || byte == 0x7FU || byte == '%' ||
                                         is_unsafe_ascii(byte) || (device && index == 0) || at_trailing;

                if (escape_byte) {
                    out.push_back('%');
                    out.push_back(hex_digits[byte >> 4U]);
                    out.push_back(hex_digits[byte & 0x0FU]);
                } else {
                    out.push_back(static_cast<char>(byte));
                }
            }
            return out;
        }

        std::string escape_fragment(const std::string_view text)
        {
            return escape_component(text);
        }

        // Split on both separators, omit empty components and escape Windows-unsafe names.
        // A path carrying no name at all gets a fixed reversible placeholder.
        std::string escape_path(const std::string_view raw)
        {
            std::string out;
            std::string component;
            const auto flush = [&out, &component]() {
                if (component.empty()) {
                    return;
                }
                if (!out.empty()) {
                    out.push_back('/');
                }
                out += escape_component(component);
                component.clear();
            };
            for (const char current : raw) {
                if (current == '/' || current == '\\') {
                    flush();
                } else {
                    component.push_back(current);
                }
            }
            flush();
            return out.empty() ? std::string(empty_name_placeholder) : out;
        }

        // The trailing extension of the last component, including the dot, or empty when the
        // component has no extension or begins with its only dot.
        std::string extension_of(const std::string_view path)
        {
            const std::string_view::size_type slash = path.find_last_of('/');
            const std::string_view::size_type dot = path.find_last_of('.');
            if (dot == npos || (slash != npos && dot < slash)) {
                return {};
            }
            const std::string_view::size_type first = slash == npos ? 0 : slash + 1;
            if (dot == first) {
                return {};
            }
            return std::string(path.substr(dot));
        }

        std::string stem_of(const std::string_view path)
        {
            const std::string extension = extension_of(path);
            return std::string(path.substr(0, path.size() - extension.size()));
        }

        std::string suffixed(const std::string_view base, const std::size_t counter)
        {
            const std::string suffix = "~" + std::to_string(counter);
            const std::string extension = extension_of(base);
            if (extension.empty()) {
                return std::string(base) + suffix;
            }
            return std::string(base.substr(0, base.size() - extension.size())) + suffix + extension;
        }

        bool is_supported_extension(const std::string_view extension)
        {
            return extension == ".png" || extension == ".webp" || extension == ".wav" || extension == ".mp3" ||
                   extension == ".ogg" || extension == ".ttf" || extension == ".otf" || extension == ".ttc" ||
                   extension == ".woff" || extension == ".woff2";
        }

        std::optional<std::string> presentation_normalize(const std::string_view raw)
        {
            std::string_view value = raw;
            if (value.starts_with("res://")) {
                value.remove_prefix(6);
            }
            if (value.empty()) {
                return std::nullopt;
            }
            if (value.front() == '/' || value.front() == '\\') {
                return std::nullopt;
            }

            std::string out;
            std::string component;
            const auto flush = [&out, &component]() {
                if (component.empty() || component == ".") {
                    component.clear();
                    return true;
                }
                if (component == "..") {
                    return false;
                }
                if (!out.empty()) {
                    out.push_back('/');
                }
                out += component;
                component.clear();
                return true;
            };
            for (const char current : value) {
                const unsigned char byte = static_cast<unsigned char>(current);
                if (byte < 0x20U || byte == 0x7FU) {
                    return std::nullopt;
                }
                if (current == ':') {
                    return std::nullopt;
                }
                if (current == '/' || current == '\\') {
                    if (!flush()) {
                        return std::nullopt;
                    }
                    continue;
                }
                component.push_back(current);
            }
            if (!flush()) {
                return std::nullopt;
            }
            if (out.empty()) {
                return std::nullopt;
            }
            return out;
        }

        // True when one manifest field byte must be percent-escaped: control bytes, DEL, the
        // escape character itself, and (only when the whole field is invalid UTF-8) every high
        // byte. Shared by escape_field and escaped_field_size so they cannot drift.
        bool field_byte_escaped(const bool valid, const unsigned char byte)
        {
            return byte < 0x20U || byte == 0x7FU || byte == '%' || (!valid && byte >= 0x80U);
        }

        // One manifest field. A valid UTF-8 field keeps its multibyte sequences and escapes
        // only control bytes and the escape character itself; a field that is not valid
        // UTF-8 falls back to escaping every non-ASCII byte, so the document stays valid
        // UTF-8 and the original bytes remain recoverable.
        std::string escape_field(const std::string_view text)
        {
            const bool valid = is_valid_utf8(text);
            std::string out;
            out.reserve(text.size());
            for (const char current : text) {
                const unsigned char byte = static_cast<unsigned char>(current);
                if (field_byte_escaped(valid, byte)) {
                    out.push_back('%');
                    out.push_back(hex_digits[byte >> 4U]);
                    out.push_back(hex_digits[byte & 0x0FU]);
                } else {
                    out.push_back(current);
                }
            }
            return out;
        }

        // Escaped byte length of one manifest field without building it, so the budget is
        // charged before escape_field allocates. Uses the same per-byte predicate, so the two
        // cannot drift.
        std::size_t escaped_field_size(const std::string_view text)
        {
            const bool valid = is_valid_utf8(text);
            std::size_t size = 0;
            for (const char current : text) {
                const unsigned char byte = static_cast<unsigned char>(current);
                size += field_byte_escaped(valid, byte) ? 3 : 1;
            }
            return size;
        }

        // Decimal digit count of a non-negative value, without allocating. Used to bound a
        // manifest row's index field before the row is appended.
        std::size_t digit_count(std::size_t value)
        {
            std::size_t digits = 1;
            while (value >= 10) {
                value /= 10;
                ++digits;
            }
            return digits;
        }

        // True when the presented path breaks the per-component or aggregate Windows limit.
        bool oversize(const std::string_view path, const std::size_t max_total)
        {
            if (utf16_length(path) > max_total) {
                return true;
            }
            std::size_t start = 0;
            for (;;) {
                const std::size_t slash = path.find('/', start);
                const std::string_view component =
                    slash == npos ? path.substr(start) : path.substr(start, slash - start);
                if (utf16_length(component) > max_component_chars) {
                    return true;
                }
                if (slash == npos) {
                    return false;
                }
                start = slash + 1;
            }
        }

        // Reserved presenter. Directory ancestors are reserved before any file is placed, so
        // a file can never clobber a directory that already owns children. Collisions are
        // resolved with a deterministic ``~N`` suffix; the collision set is finite, so a free
        // suffix or an over-limit suffix is always reached within a bounded number of tries.
        class placer final
        {
          public:
            placer(budget &pool, const tree_limits &limits) : pool_(pool), limits_(limits)
            {
            }

            void reserve_dir(const std::string_view path)
            {
                insert_name(dirs_, path, pool_);
            }

            void reserve_ancestors(const std::string_view path)
            {
                std::size_t position = 0;
                for (;;) {
                    const std::size_t slash = path.find('/', position);
                    if (slash == npos) {
                        return;
                    }
                    reserve_dir(path.substr(0, slash));
                    position = slash + 1;
                }
            }

            [[nodiscard]] bool taken(const std::string_view path) const
            {
                const std::wstring key = widen(path);
                return files_.find(key) != files_.end() || dirs_.find(key) != dirs_.end();
            }

            [[nodiscard]] bool fits(const std::string_view path) const
            {
                return !oversize(path, limits_.max_path_chars);
            }

            std::string place(const std::string &preferred, const std::string &fallback)
            {
                const std::optional<std::string> chosen = select(preferred);
                if (chosen.has_value()) {
                    return accept(*chosen);
                }
                const std::optional<std::string> rescued = select(fallback);
                if (!rescued.has_value()) {
                    throw tree_error("godot virtual tree path limit leaves no room for a fallback");
                }
                return accept(*rescued);
            }

          private:
            // First free candidate: ``base`` then ``base~1``, ``base~2``... until the suffix
            // pushes the name past a limit. Termination is guaranteed because the suffixes are
            // distinct and the reserved-name set is finite. A base that has actually collided
            // remembers where its search stopped: because the reserved-name sets only ever
            // grow, the first free suffix for a base is non-decreasing, so a run of identical
            // names resolves in constant time instead of re-scanning from zero, while distinct
            // names (which never collide) allocate no progression entry at all.
            [[nodiscard]] std::optional<std::string> select(const std::string &base)
            {
                std::wstring key = widen(base);
                std::size_t counter = 0;
                const auto remembered = suffix_next_.find(key);
                if (remembered != suffix_next_.end()) {
                    counter = remembered->second;
                }
                for (;;) {
                    std::string candidate = counter == 0 ? base : suffixed(base, counter);
                    if (!fits(candidate)) {
                        return std::nullopt;
                    }
                    ++counter;
                    if (!taken(candidate)) {
                        if (counter > 1) {
                            const auto existing = suffix_next_.find(key);
                            if (existing == suffix_next_.end()) {
                                pool_.charge(key.size() * 2 + map_node_overhead);
                                suffix_next_.emplace(std::move(key), counter);
                            } else {
                                existing->second = counter;
                            }
                        }
                        return candidate;
                    }
                }
            }

            std::string accept(const std::string &candidate)
            {
                insert_name(files_, candidate, pool_);
                return candidate;
            }

            budget &pool_;
            tree_limits limits_;
            name_set files_;
            name_set dirs_;
            std::map<std::wstring, std::size_t, ordinal_less> suffix_next_;
        };

        struct view_row final
        {
            std::string effective;
            std::string fallback;
            std::size_t index = 0;
        };
    } // namespace

    virtual_tree make_virtual_tree(const pck_index &index, const std::span<const import_mapping> mappings,
                                   const std::span<const conversion_hint> hints, const tree_limits &limits)
    {
        budget pool(limits.max_metadata_bytes);

        // Structural preflight. Counts and a necessary metadata lower bound are checked with
        // division (which cannot overflow) before any input-driven allocation happens, so a
        // tiny budget fails before the map/index/name copies below are built.
        const std::size_t raw_count = index.entries.size();
        if (raw_count > limits.max_entries || mappings.size() > limits.max_entries ||
            hints.size() > limits.max_entries) {
            throw tree_error("godot virtual tree exceeds the entry budget");
        }
        if (raw_count > limits.max_metadata_bytes / entry_slot_overhead) {
            throw tree_budget_error("godot virtual tree exceeds the metadata budget");
        }

        std::unordered_map<std::size_t, std::string> hint_by_index;
        for (const conversion_hint &hint : hints) {
            if (hint.source_index >= raw_count || index.entries[hint.source_index].kind != pck_entry_kind::file) {
                throw tree_error("conversion hint source index is out of range");
            }
            if (!is_supported_extension(hint.extension)) {
                throw tree_error("conversion hint extension is unsupported");
            }
            const auto existing = hint_by_index.find(hint.source_index);
            if (existing == hint_by_index.end()) {
                pool.charge(hint.extension.size() + map_node_overhead);
                hint_by_index.emplace(hint.source_index, hint.extension);
            } else if (existing->second != hint.extension) {
                throw tree_error("conversion hint contradicts an earlier hint");
            }
        }

        struct normalized_mapping final
        {
            std::string virtual_path;
            std::string compiled_path;
            std::string variant;
        };

        std::vector<normalized_mapping> maps;
        for (const import_mapping &mapping : mappings) {
            // Charge borrowed input before normalizing or retaining its owning copy.
            pool.charge(mapping.virtual_path.size() + mapping.compiled_path.size() + mapping.variant.size() +
                        sizeof(normalized_mapping) + row_node_overhead);
            const auto virtual_path = presentation_normalize(mapping.virtual_path);
            const auto compiled_path = presentation_normalize(mapping.compiled_path);
            if (!virtual_path.has_value() || !compiled_path.has_value()) {
                throw tree_error("import/remap mapping path is invalid");
            }
            maps.push_back(normalized_mapping{*virtual_path, *compiled_path, mapping.variant});
        }

        std::unordered_map<std::string, std::size_t> first_with_path;
        for (std::size_t position = 0; position < raw_count; ++position) {
            if (index.entries[position].kind != pck_entry_kind::file) {
                continue;
            }
            const std::string &physical = index.entries[position].path;
            if (!first_with_path.contains(physical)) {
                pool.charge(physical.size() + map_node_overhead);
                first_with_path.emplace(physical, position);
            }
        }

        // The first mapping selects a raw fallback's folder and basename. Supported sources use
        // converted aliases instead; multiple metadata mappings may restore several logical names.
        std::unordered_map<std::size_t, std::size_t> first_mapping;
        for (std::size_t position = 0; position < maps.size(); ++position) {
            const auto target = first_with_path.find(maps[position].compiled_path);
            if (target != first_with_path.end() && !first_mapping.contains(target->second)) {
                pool.charge(map_node_overhead + sizeof(std::size_t) * 2);
                first_mapping.emplace(target->second, position);
            }
        }

        // A missing extension means preserve the complete physical name. A present extension,
        // including an empty one, replaces the virtual source extension with the actual encoding.
        const auto presented_path = [&limits](const std::string_view path, const std::string_view variant,
                                              const std::optional<std::string_view> extension,
                                              const std::string &fallback) {
            if (utf16_length(path) > limits.max_path_chars) {
                return fallback;
            }
            std::string preferred = escape_path(path);
            if (extension.has_value()) {
                preferred = stem_of(preferred);
                if (!variant.empty()) {
                    preferred += "." + escape_fragment(variant);
                }
                preferred += escape_fragment(*extension);
            }
            return oversize(preferred, limits.max_path_chars) ? fallback : preferred;
        };

        std::vector<view_row> raw_rows;
        pool.reserve_slots(raw_count, sizeof(view_row));
        raw_rows.reserve(raw_count);
        for (std::size_t position = 0; position < raw_count; ++position) {
            if (index.entries[position].kind != pck_entry_kind::file || hint_by_index.contains(position)) {
                continue;
            }
            view_row row;
            row.index = position;
            row.fallback = std::to_string(position);
            const auto mapped = first_mapping.find(position);
            if (mapped == first_mapping.end()) {
                row.effective = presented_path(index.entries[position].path, {}, std::nullopt, row.fallback);
            } else {
                const auto &mapping = maps[mapped->second];
                const std::string extension = extension_of(mapping.compiled_path);
                row.effective = presented_path(mapping.virtual_path, mapping.variant, extension, row.fallback);
            }
            pool.charge(row.effective.size() + row.fallback.size() + row_node_overhead);
            raw_rows.push_back(std::move(row));
        }

        std::vector<view_row> converted_rows;
        const auto add_converted = [&](const std::size_t position, const std::string_view path,
                                       const std::string_view variant, const std::string_view extension) {
            view_row row;
            row.index = position;
            row.fallback = std::to_string(raw_count + converted_rows.size());
            row.effective = presented_path(path, variant, extension, row.fallback);
            pool.charge(row.effective.size() + row.fallback.size() + sizeof(view_row) + row_node_overhead);
            converted_rows.push_back(std::move(row));
        };
        for (std::size_t position = 0; position < raw_count; ++position) {
            const auto hint = hint_by_index.find(position);
            if (hint != hint_by_index.end() && !first_mapping.contains(position)) {
                add_converted(position, index.entries[position].path, {}, hint->second);
            }
        }
        for (const auto &mapping : maps) {
            const auto target = first_with_path.find(mapping.compiled_path);
            if (target == first_with_path.end()) {
                continue;
            }
            const auto hint = hint_by_index.find(target->second);
            if (hint != hint_by_index.end()) {
                add_converted(target->second, mapping.virtual_path, mapping.variant, hint->second);
            }
        }

        placer tree(pool, limits);
        for (const auto &row : raw_rows) {
            tree.reserve_ancestors(row.effective);
        }
        for (const auto &row : converted_rows) {
            tree.reserve_ancestors(row.effective);
        }

        std::vector<virtual_entry> entries;
        const auto add_entry = [&entries, &pool, &limits](std::string path,
                                                          const std::optional<std::size_t> source_index,
                                                          const bool converted, std::string generated_text) {
            if (entries.size() >= limits.max_entries) {
                throw tree_error("godot virtual tree exceeds the entry budget");
            }
            pool.charge(path.size() + generated_text.size() + entry_slot_overhead);
            entries.push_back(virtual_entry{std::move(path), source_index, converted, std::move(generated_text)});
        };

        std::string manifest_text;
        const auto append_row = [&](const view_row &row, const bool converted) {
            const std::string final_path = tree.place(row.effective, row.fallback);
            const std::string &physical = index.entries[row.index].path;
            if (final_path != physical) {
                // The mapping always points to a visible row, whether decoded or raw.
                const std::size_t row_bound =
                    escaped_field_size(physical) + escaped_field_size(final_path) + digit_count(row.index) + 3;
                pool.charge(row_bound);
                manifest_text.reserve(manifest_text.size() + row_bound);
                manifest_text += std::to_string(row.index);
                manifest_text.push_back('\t');
                manifest_text += escape_field(physical);
                manifest_text.push_back('\t');
                manifest_text += escape_field(final_path);
                manifest_text.push_back('\n');
            }
            add_entry(final_path, row.index, converted, {});
        };
        for (const auto &row : raw_rows) {
            append_row(row, false);
        }
        for (const auto &row : converted_rows) {
            append_row(row, true);
        }
        std::string removal_text;
        for (std::size_t position = 0; position < raw_count; ++position) {
            if (index.entries[position].kind != pck_entry_kind::removal) {
                continue;
            }
            const auto &physical = index.entries[position].path;
            const std::size_t row_bound = digit_count(position) + escaped_field_size(physical) + 2;
            pool.charge(row_bound);
            removal_text.reserve(removal_text.size() + row_bound);
            removal_text += std::to_string(position);
            removal_text.push_back('\t');
            removal_text += escape_field(physical);
            removal_text.push_back('\n');
        }
        if (!removal_text.empty()) {
            add_entry(tree.place("patch-removals.tsv", "r"), std::nullopt, false, std::move(removal_text));
        }
        if (!manifest_text.empty()) {
            // Place after physical files so generating a manifest never renames an unchanged
            // source. The same collision rules protect directory ancestors and case variants.
            add_entry(tree.place(std::string(manifest_name), "n"), std::nullopt, false, std::move(manifest_text));
        }
        return virtual_tree{std::move(entries)};
    }
} // namespace unpackrat::godot
